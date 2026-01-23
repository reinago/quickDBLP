#include <iostream>
#include <iomanip>
#include <fstream>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>
#include <array>
#include <tuple>
#include <regex>
#include <filesystem>

// #define USE_BOOST_IOSTREAMS
// TODO
// profile the code
// is it better collect everything per thread and merge afterward? ID merging would be a headache
#define USE_THREAD_POOL 1

#ifndef USE_BOOST_IOSTREAMS
#define WITH_GZFILEOP
#include <zlib-ng.h>
#else
#include <boost/iostreams/filtering_stream.hpp>
#include <boost/iostreams/filter/gzip.hpp>
#include <boost/iostreams/device/file.hpp>
#include <boost/iostreams/device/file_descriptor.hpp>
#endif

#include "InMemDB.hpp"
#include "ThreadPool.hpp"
#include "ThreadSafeIDGenerator.hpp"
#include "Timer.hpp"

// Utility functions for logging
enum class LogLevel { None, Error, Warning, Info };
LogLevel printLevel = LogLevel::Warning;

void printInfo(const std::string& msg) {
	if (printLevel >= LogLevel::Info) {
		std::cout << "INFO: " << msg << std::endl;
	}
}

void printWarning(const std::string& msg) {
	if (printLevel >= LogLevel::Warning) {
		std::cout << "WARNING: " << msg << std::endl;
	}
}

void printError(const std::string& msg) {
	if (printLevel >= LogLevel::Error) {
		std::cerr << "ERROR: " << msg << std::endl;
	}
}

ThreadSafeIDGenerator<uint32_t> authorsToNumbers;
struct Author {
	std::string id;
	std::string orcid;
	std::string name;
};
InMemDB<uint32_t, Author> authorDB;

ThreadSafeIDGenerator<uint32_t> papersToNumbers;
struct Paper {
	std::string id;
	std::string title;
	uint8_t type = 0;
	uint16_t year = 0;
};
InMemDB<uint32_t, Paper> paperDB;

LinkDB<uint32_t> papersAndAuthorsDB;

class ParserState {
public:
	enum Value : uint8_t {
		Searching = 0,
		Inproceedings,
		Incollection,
		Article,
		Book,
		Part,
		Informal,
		Data,
		NUMBER_OF_PARSER_STATES
	};

	inline static std::string startingPrefix = "<dblp:";
	inline static std::string endingPrefix = "</dblp:";

	constexpr operator Value() const {
		return state;
	}

	static std::string getEntityFromState(Value st) {
		switch (st) {
		case Inproceedings:
			return "Inproceedings";
		case Incollection:
			return "Incollection";
		case Article:
			return "Article";
		case Book:
			return "Book";
		case Part:
			return "Part";
		case Informal:
			return "Informal";
		case Data:
			return "Data";
		default:
			return "Unknown";
		}
	}

	std::string getEntity() const {
		return getEntityFromState(state);
	}

	static std::string getStartSnippet(Value st) {
		return startingPrefix + getEntityFromState(st);
	}

	static std::string getEndSnippet(Value st) {
		return endingPrefix + getEntityFromState(st) + ">";
	}

	std::string getStartSnippet() const {
		return getStartSnippet(state);
	}
	std::string getEndSnippet() const {
		return getEndSnippet(state);
	}

	void checkForStateChange(const std::string& line) {
		if (state == Searching) {
			if (line.find(startingPrefix) == std::string::npos) {
				return;
			}
			for (int i = 1; i < NUMBER_OF_PARSER_STATES; ++i) {
				if (line.find(getEntityFromState(static_cast<Value>(i)), startingPrefix.size()) == startingPrefix.size()) {
					state = static_cast<Value>(i);
					return;
				}
			}
		} else {
			if (line.find(endingPrefix) == std::string::npos) {
				return;
			}
			if (line.find(getEntity(), endingPrefix.size()) == endingPrefix.size()) {
				state = Searching;
			}
		}
	}

private:
	Value state = Searching;
};

// Helper functions
int checkAuthor(const std::string& authorID, const std::string& authorOrcid, const std::string& authorName) {
	auto res = authorsToNumbers.getOrCreateID(authorID);
	int realID = std::get<0>(res);
	if (std::get<1>(res)) {
		// New author, add to the database
		authorDB.storeItem(realID, { authorID, authorOrcid, authorName });
		printInfo("Assigned number " + std::to_string(realID) + " to author ID " + authorID);
	} else {
		// Existing author
		printInfo("Existing author found: " + authorID);
	}
	return realID;
}

int processPaperBuffer(std::vector<std::string> buf, ParserState::Value paperType) {
	static std::regex idRegex(R"q(rdf:about="([^"]+)")q");
	static std::regex titleRegex(R"q(<dblp:title>([^<]+)</dblp:title>)q");
	static std::regex yearRegex(R"q(<dblp:yearOfPublication.*?>(\d+)</dblp:yearOfPublication>)q");
	static std::regex authorRegex(R"q(<dblp:authoredBy rdf:resource="([^"]+)")q");
	static std::regex signatureCreatorRegex(R"q(<dblp:signatureCreator rdf:resource="([^"]+)")q");
	static std::regex signatureOrcidRegex(R"q(<dblp:signatureOrcid rdf:resource="([^"]+)")q");
	static std::regex signatureNameRegex(R"q(<dblp:signatureDblpName>([^<]+)</dblp:signatureDblpName>)q");

	std::smatch match;

	std::vector<std::tuple<std::string, std::string, std::string>> currCreators;
	std::vector<std::string> currAuthors;
	std::string currPaperID;
	uint32_t currPaperNumericID = 0;
	std::string currTitle;
	uint16_t currYear = 0;
	std::string authorID, authorOrcid, authorName;

	bool iteratingAuthors = false;

	for (const auto& line : buf) {
		if (line.find(ParserState::getStartSnippet(paperType)) == 0) {
			// started a paper entry
			if (std::regex_search(line, match, idRegex)) {
				currPaperID = match[1];
				printInfo("Current ID: " + currPaperID);
				auto res = papersToNumbers.getOrCreateID(currPaperID);
				currPaperNumericID = std::get<0>(res);
				printInfo("Assigned number " + std::to_string(currPaperNumericID) + " to paper ID " + currPaperID);
			} else {
				printError("No ID found in entry: " + line);
			}
		} else if (line.find(ParserState::getEndSnippet(paperType)) == 0) {
			// end of the paper entry
			if (currAuthors.size() != currCreators.size()) {
				printError("Number of authors and creators do not match for paper " + currPaperID + ": " +
					std::to_string(currAuthors.size()) + " vs " + std::to_string(currCreators.size()));
			} else {
				for (const auto& [authorID, authorOrcid, authorName] : currCreators) {
					int realID = checkAuthor(authorID, authorOrcid, authorName);
					papersAndAuthorsDB.storeLink(currPaperNumericID, realID);
				}
			}
			paperDB.storeItem(currPaperNumericID, { currPaperID, currTitle, paperType, currYear });
		} else {
			// analyze the paper entry
			if (std::regex_search(line, match, titleRegex)) {
				currTitle = match[1];
				printInfo("Current Title: " + currTitle);
			} else if (std::regex_search(line, match, yearRegex)) {
				currYear = static_cast<uint16_t>(std::stoi(match[1]));
				printInfo("Current Year: " + std::to_string(currYear));
			} else if (std::regex_search(line, match, authorRegex)) {
				printInfo("Found author ID: " + match[1].str());
				currAuthors.push_back(match[1]);
			} else if (line.find("<dblp:AuthorSignature") != std::string::npos) {
				iteratingAuthors = true;
			}
			if (iteratingAuthors) {
				if (line.find("</dblp:AuthorSignature") != std::string::npos) {
					iteratingAuthors = false;
					currCreators.emplace_back(authorID, authorOrcid, authorName);
					printInfo("Found creator: " + authorID + ", " + authorOrcid + ", " + authorName);
				} else {
					if (std::regex_search(line, match, signatureCreatorRegex)) {
						authorID = match[1];
					} else if (std::regex_search(line, match, signatureOrcidRegex)) {
						authorOrcid = match[1];
					} else if (std::regex_search(line, match, signatureNameRegex)) {
						authorName = match[1];
					}
				}
			}
		}
	} 
	return 0;
}

void checkProgress(uint64_t current, uint64_t total) {
	static uint64_t lastProgress = 0;
	const int barWidth = 70;

	if (current - lastProgress > 1) {
		auto progress = static_cast<double>(current) / total;
		std::cout << "[";
		int pos = barWidth * progress;
		for (int i = 0; i < barWidth; ++i) {
			if (i < pos) std::cout << "=";
			else if (i == pos) std::cout << ">";
			else std::cout << " ";
		}
		std::cout << "] " << std::setfill(' ') << std::setw(3) << int(progress * 100.0) << " %\r";
		//std::cout.flush();

		lastProgress = current;
	}
}

void dumpData() {
	// File streams
	std::ofstream papersFile("dblp_papers.csv");
	std::ofstream authorsFile("dblp_authors.csv");
	std::ofstream papersAuthorsFile("dblp_papers_authors.csv");

	// Initialize files
	papersFile << "NumericID\tDBLP\tTitle\tType\tYear\n";
	authorsFile << "NumericID\tDBLP\tName\tORCID\n";
	papersAuthorsFile << "PaperID\tAuthorID\n";

	std::cout << "Dumping papers..." << std::endl;
	for (auto p = 1; p < papersToNumbers.getMaxID(); ++p) {
		if (p % 100000 == 0) {
			checkProgress(p, papersToNumbers.getMaxID());
		}
		auto paper = paperDB.getItem(p);
		papersFile << p << "\t" << paper.id << "\t" << paper.title << "\t" << unsigned(paper.type) << "\t" << paper.year << "\n";
	}
	std::cout << std::endl << "Dumping authors..." << std::endl;
	for (auto a = 1; a < authorsToNumbers.getMaxID(); ++a) {
		if (a % 100000 == 0) {
			checkProgress(a, authorsToNumbers.getMaxID());
		}
		auto author = authorDB.getItem(a);
		authorsFile << a << "\t" << author.id << "\t" << author.name << "\t" << author.orcid << "\n";
	}
	std::cout << std::endl << "Dumping relations..." << std::endl;
	for (auto r = 1; r < papersAndAuthorsDB.size(); ++r) {
		if (r % 100000 == 0) {
			checkProgress(r, papersAndAuthorsDB.size());
		}
		auto link = papersAndAuthorsDB.getItem(r);
		papersAuthorsFile << link.first << "\t" << link.second << "\n";
	}
	std::cout << std::endl << "Dumping completed." << std::endl;

	// Close files
	papersFile.close();
	authorsFile.close();
	papersAuthorsFile.close();
}

#ifndef USE_BOOST_IOSTREAMS
void checkGZProgress(uint64_t lineCount, gzFile file, uint64_t total) {
	if (lineCount % 1000000 == 0) {
		auto pos = zng_gztell(file);
		if (pos > -1) {
			checkProgress(static_cast<uint64_t>(pos), total);
		}
	}
}
#else
void checkBOOSTProgress(uint64_t lineCount, boost::iostreams::file_descriptor& fd, uint64_t total) {
	if (lineCount % 100000 == 0) {
		std::streampos pos = fd.seek(0, std::ios::cur);
		checkProgress(static_cast<uint64_t>(pos), total);
	}
}
#endif

bool checkLockFile(const std::string& lockFilePath) {
	if (std::filesystem::exists(lockFilePath)) {
		std::cerr << "Lock file exists. Another instance may be running. Exiting.";
		return false;
	}
	// Create lock file
	std::ofstream lockFile(lockFilePath);
	if (!lockFile) {
		std::cerr << "Failed to create lock file. Exiting.";
		return false;
	}
	lockFile << "Lock file for ponder_dblp. Remove this file only if you are SURE no other instance is running.\n";
	return true;
}

void removeLockFile(const std::string& lockFilePath) {
	if (std::filesystem::exists(lockFilePath)) {
		std::filesystem::remove(lockFilePath);
	}
}

int main() {
	// hack for broken visual studio cwd
	//std::filesystem::current_path("h:\\src\\quickDBLP");

	//const std::string inputFilePath = "mini.rdf.gz";
	const std::string inputFilePath = "dblp.rdf.gz";
	const std::string lockFilePath = inputFilePath + ".lock";

	try {
		if (!checkLockFile(lockFilePath)) {
			return 1;
		}

		std::vector<char> hugebuf;
		hugebuf.resize(1024 * 1024 * 256);

#ifdef USE_BOOST_IOSTREAMS
		boost::iostreams::filtering_istream inputFile;
		inputFile.push(boost::iostreams::gzip_decompressor());
		auto fd = boost::iostreams::file_descriptor(inputFilePath);
		inputFile.push(fd);
		auto fd2 = boost::iostreams::file_descriptor(inputFilePath);
		fd2.seek(0, std::ios::end);
		std::streampos fileSizeBOOST = fd2.seek(0, std::ios::cur);
#else
		uint64_t fileSizeGZ = 0;
		auto file = zng_gzopen(inputFilePath.c_str(), "rb");
		{
			Timer timer("Checking database size...");
			if (!file) {
				std::cerr << "Failed to open file with zlib: " << inputFilePath << "\n";
				removeLockFile(lockFilePath);
				return 1;
			}
			int numRead = 0;
			// start timer

			while ((numRead = zng_gzread(file, hugebuf.data(), hugebuf.size())) > 0) {
				fileSizeGZ += numRead;
			}
			zng_gzclose(file);
			std::cout << "File size (zlib): " << fileSizeGZ << " bytes\n";
		}
		
		file = zng_gzopen(inputFilePath.c_str(), "rb");
#endif
		{
			Timer timer("Processing database dump...");
			// Process file
			ThreadPool threadPool(std::thread::hardware_concurrency() * 2);
			std::string line;
			uint64_t lineCount = 0;
			int state = 0; // 0 = searching, 1 = inproceedings, 3 = article
			ParserState parserState, oldState;
			std::vector<std::string> paperBuffer;

#ifdef USE_BOOST_IOSTREAMS
			while (std::getline(inputFile, line)) {
				checkBOOSTProgress(++lineCount, fd, fileSizeBOOST);
#else
			while (zng_gzgets(file, hugebuf.data(), hugebuf.size()) != nullptr) {
				line = hugebuf.data();
				checkGZProgress(++lineCount, file, fileSizeGZ);
#endif
				parserState.checkForStateChange(line);
				if (parserState != oldState) {
					if (oldState != ParserState::Searching) {
						printInfo("End of " + oldState.getEntity() + " entry");
						paperBuffer.push_back(line);
						ParserState::Value paperType = oldState;
#ifdef USE_THREAD_POOL
						threadPool.enqueue([paperBuffer, paperType]() { processPaperBuffer(paperBuffer, paperType); });
#else
						processPaperBuffer(paperBuffer, paperType);
#endif
					} else {
						printInfo("Found " + parserState.getEntity() + " entry");
						paperBuffer.clear();
						paperBuffer.push_back(line);
					}
					oldState = parserState;
				} else {
					if (parserState != ParserState::Searching) {
						paperBuffer.push_back(line);
					}
				}
			}
			std::cout << std::endl << "Waiting for threads to finish parsing..." << std::endl;
			threadPool.waitForAll();
		}
		std::cout << "saving CSVs..." << std::endl;
		dumpData();
#ifndef USE_BOOST_IOSTREAMS
		zng_gzclose(file);
#endif
	} catch (const std::exception& e) {
		printError("Exception: " + std::string(e.what()));
		removeLockFile(lockFilePath);
		return 1;
	} catch (...) {
		printError("Unknown exception occurred.");
		removeLockFile(lockFilePath);
		return 1;
	}
	removeLockFile(lockFilePath);
	return 0;
}