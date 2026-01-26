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

// TODO
// profile the code
// is it better collect everything per thread and merge afterward? ID merging would be a headache
#define DEBUGGING 1
#define USE_THREAD_POOL 1

#define WITH_GZFILEOP
#include <zlib-ng.h>

#include <pugixml.hpp>

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

std::string dump_node(pugi::xml_node n) {
	std::ostringstream oss;
	n.print(oss);
	return oss.str();
}

pugi::xml_node check_child(pugi::xml_node n, std::string name) {
	auto c = n.child(name);
	if (c == nullptr) throw std::invalid_argument("could not find '" + name + "' in '" + n.name() + "'\n" + dump_node(n));
	return c;
};

std::string check_attribute(pugi::xml_node n, std::string name) {
	auto r = n.attribute(name);
	if (r == nullptr) return "";
	return r.value();
};

std::string check_resource(pugi::xml_node n) {
	return check_attribute(n, "rdf:resource");
};

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

	std::ostringstream imploded;
	std::copy(buf.begin(), buf.end(),
		std::ostream_iterator<std::string>(imploded, "\n"));
	pugi::xml_document doc;
	auto result = doc.load_string(imploded.str().c_str());

	if (result.status != pugi::status_ok) {
		throw std::invalid_argument("could not parse publication: " + buf[0]);
	}

	auto pub = *doc.children().begin(); //doc.child("dblp:" + ParserState::getEntityFromState(paperType));
	currPaperID = check_attribute(pub, "rdf:about");
	if (currPaperID.empty()) {
		printWarning("No ID found, skipping:\n" + dump_node(pub));
		return 0;
	}
	printInfo("Current ID: " + currPaperID);
	auto res = papersToNumbers.getOrCreateID(currPaperID);
	currPaperNumericID = std::get<0>(res);
	printInfo("Assigned number " + std::to_string(currPaperNumericID) + " to paper ID " + currPaperID);

	for (pugi::xml_node author: pub.children("dblp:authoredBy")) {
		auto res = check_resource(author);
		currAuthors.push_back(res);
	}
	for (pugi::xml_node sig: pub.children("dblp:hasSignature")) {
		for (pugi::xml_node sig_content : sig.children("dblp:AuthorSignature")) {
			authorID = check_resource(check_child(sig_content, "dblp:signatureCreator"));
			try {
				authorOrcid = check_resource(check_child(sig_content, "dblp:signatureOrcid"));
			} catch (const std::invalid_argument&) {
				authorOrcid = "";
			}
			authorName = check_child(sig_content, "dblp:signatureDblpName").child_value();
			printInfo("Found creator: " + authorID + ", " + authorOrcid + ", " + authorName);
			currCreators.emplace_back(authorID, authorOrcid, authorName);
		}
	}

	if (currAuthors.size() != currCreators.size()) {
		printError("Number of authors and creators do not match for paper " + currPaperID + ": " +
			std::to_string(currAuthors.size()) + " vs " + std::to_string(currCreators.size()));
		return 0;
	}
	if (currAuthors.size() == 0) {
		printInfo("No authors found for paper " + currPaperID + ", skipping");
		// printWarning("\n" + dump_node(pub));
		return 0;
	}
	auto child_title = pub.child("dblp:title");
	if (child_title == nullptr) {
		printWarning("No title found for paper " + currPaperID + ", skipping\n" + dump_node(pub));
		return 0;
	}
	currTitle = child_title.child_value();
	printInfo("Current Title: " + currTitle);

	auto child_year = pub.child("dblp:yearOfPublication");
	if (child_year == nullptr) {
		child_year = pub.child("dblp:yearOfEvent");
		if (child_year == nullptr) {
			printWarning("No year found for paper " + currPaperID + ", skipping\n" + dump_node(pub));
			return 0;
		}
	}
	currYear = static_cast<uint16_t>(std::stoi(child_year.child_value()));
	printInfo("Current Year: " + std::to_string(currYear));

	for (const auto& [authorID, authorOrcid, authorName] : currCreators) {
		int realID = checkAuthor(authorID, authorOrcid, authorName);
		papersAndAuthorsDB.storeLink(currPaperNumericID, realID);
	}
	paperDB.storeItem(currPaperNumericID, { currPaperID, currTitle, paperType, currYear });

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

void checkGZProgress(uint64_t lineCount, gzFile file, uint64_t total) {
	if (lineCount % 1000000 == 0) {
		auto pos = zng_gztell(file);
		if (pos > -1) {
			checkProgress(static_cast<uint64_t>(pos), total);
		}
	}
}

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
#ifdef DEBUGGING
	// hack for broken visual studio cwd
	std::filesystem::current_path("u:\\src\\quickDBLP");
#endif

	//const std::string inputFilePath = "mini.rdf.gz";
	const std::string inputFilePath = "dblp.rdf.gz";
	const std::string lockFilePath = inputFilePath + ".lock";

	try {
#ifndef DEBUGGING
		if (!checkLockFile(lockFilePath)) {
			return 1;
		}
#endif

		std::vector<char> hugebuf;
		hugebuf.resize(1024 * 1024 * 256);

		auto file = zng_gzopen(inputFilePath.c_str(), "rb");
#ifdef DEBUGGING
		uint64_t fileSizeGZ = 38980527145;
#else
		uint64_t fileSizeGZ = 0;
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
#endif
		
		file = zng_gzopen(inputFilePath.c_str(), "rb");
		{
			Timer timer("Processing database dump...");
			// Process file
			ThreadPool threadPool(std::thread::hardware_concurrency() * 2);
			std::string line;
			uint64_t lineCount = 0;
			int state = 0; // 0 = searching, 1 = inproceedings, 3 = article
			ParserState parserState, oldState;
			std::vector<std::string> paperBuffer;

			while (zng_gzgets(file, hugebuf.data(), hugebuf.size()) != nullptr) {
				line = hugebuf.data();
				checkGZProgress(++lineCount, file, fileSizeGZ);
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