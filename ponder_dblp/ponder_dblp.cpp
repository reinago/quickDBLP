#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>
#include <tuple>
#include <regex>

#include <boost/iostreams/filtering_stream.hpp>
#include <boost/iostreams/filter/gzip.hpp>
#include <boost/iostreams/device/file.hpp>
#include <boost/iostreams/device/file_descriptor.hpp>

#include "ThreadSafeIDGenerator.hpp"
#include "InMemDB.hpp"
#include "ThreadPool.hpp"

//#define USE_THREAD_POOL 1

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



// Global variables
//std::unordered_map<std::string, int> papersToNumbers;
//int maxPaperNumber = 1;

//std::unordered_map<std::string, int> authorsToNumbers;
//int maxAuthorNumber = 1;
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
	int year;
};
InMemDB<uint32_t, Paper> paperDB;

LinkDB<uint32_t> papersAndAuthorsDB;

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

int processPaperBuffer(std::vector<std::string> buf) {
	static std::regex idRegex(R"q(rdf:about="([^"]+)")q");
	static std::regex titleRegex(R"q(<dblp:title>([^<]+)</dblp:title>)q");
	static std::regex yearRegex(R"q(<dblp:yearOfPublication.*?>(\d+)</dblp:yearOfPublication>)q");
	static std::regex authorRegex(R"q(<dblp:authoredBy rdf:resource="([^"]+)")q");
	static std::regex signatureCreatorRegex(R"q(<dblp:signatureCreator rdf:resource="([^"]+)")q");
	static std::regex signatureOrcidRegex(R"q(<dblp:signatureOrcid rdf:resource="([^"]+)")q");
	static std::regex signatureNameRegex(R"q(<dblp:signatureDblpName>([^<]+)</dblp:signatureDblpName>)q");

	std::vector<std::tuple<std::string, std::string, std::string>> currCreators;
	std::vector<std::string> currAuthors;
	std::string currPaperID;
	uint32_t currPaperNumericID = 0;
	std::string currTitle;
	int currYear = 0;
	std::string authorID, authorOrcid, authorName;

	bool iteratingAuthors = false;

	for (const auto& line : buf) {
		if (line.find("<dblp:Inproceedings") != std::string::npos || line.find("<dblp:Article") != std::string::npos) {
			// started a paper entry
			std::smatch match;
			if (std::regex_search(line, match, idRegex)) {
				currPaperID = match[1];
				printInfo("Current ID: " + currPaperID);
				auto res = papersToNumbers.getOrCreateID(currPaperID);
				currPaperNumericID = std::get<0>(res);
				printInfo("Assigned number " + std::to_string(currPaperNumericID) + " to paper ID " + currPaperID);
			} else {
				printError("No ID found in entry: " + line);
			}
		} else if (line.find("</dblp:Inproceedings") != std::string::npos || line.find("</dblp:Article") != std::string::npos) {
			// end of the paper entry
			if (currAuthors.size() != currCreators.size()) {
				printError("Number of authors and creators do not match for paper " + currPaperID + ": " +
					std::to_string(currAuthors.size()) + " vs " + std::to_string(currCreators.size()));
			} else {
				for (const auto& [authorID, authorOrcid, authorName] : currCreators) {
					int realID = checkAuthor(authorID, authorOrcid, authorName);
					//papersAuthorsFile << maxPaperNumber - 1 << "\t" << realID << "\n";
					papersAndAuthorsDB.storeLink(currPaperNumericID, realID);
				}
			}
			paperDB.storeItem(currPaperNumericID, { currPaperID, currTitle, currYear });
			//papersFile << maxPaperNumber - 1 << "\t" << currPaperID << "\t" << currTitle << "\t" << currYear << "\n";
		} else {
			// analyze the paper entry
			std::smatch match;
			if (std::regex_search(line, match, titleRegex)) {
				currTitle = match[1];
				printInfo("Current Title: " + currTitle);
			} else if (std::regex_search(line, match, yearRegex)) {
				currYear = std::stoi(match[1]);
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

void dumpData() {
	// File streams
	std::ofstream papersFile("dblp_papers.csv");
	std::ofstream authorsFile("dblp_authors.csv");
	std::ofstream papersAuthorsFile("dblp_papers_authors.csv");

	// Initialize files
	papersFile << "NumericID\tDBLP\tTitle\tYear\n";
	authorsFile << "NumericID\tDBLP\tName\tORCID\n";
	papersAuthorsFile << "PaperID\tAuthorID\n";

	for (uint32_t p = 1; p < papersToNumbers.getMaxID(); ++p) {
		auto paper = paperDB.getItem(p);
		papersFile << p << "\t" << paper.id << "\t" << paper.title << "\t" << paper.year << "\n";
	}
	for (uint32_t a = 1; a < authorsToNumbers.getMaxID(); ++a) {
		auto author = authorDB.getItem(a);
		authorsFile << a << "\t" << author.id << "\t" << author.name << "\t" << author.orcid << "\n";
	}
	for (auto it = papersAndAuthorsDB.begin(); it != papersAndAuthorsDB.end(); ++it) {
		papersAuthorsFile << it->first << "\t" << it->second << "\n";
	}

	// Close files
	papersFile.close();
	authorsFile.close();
	papersAuthorsFile.close();
}

int main() {

	try {
		// Open gzipped input file using Boost.Iostreams
		boost::iostreams::filtering_istream inputFile;
		inputFile.push(boost::iostreams::gzip_decompressor());
		inputFile.push(boost::iostreams::file_descriptor("dblp.rdf.gz"));

		ThreadPool threadPool(std::thread::hardware_concurrency() * 2);

		// Process file
		std::string line;
		int state = 0; // 0 = searching, 1 = inproceedings, 3 = article
		std::vector<std::string> paperBuffer;
		std::cout << "Processing database dump..." << std::endl;
		while (std::getline(inputFile, line)) {
			if (state == 0) {
				if (line.find("<dblp:Inproceedings") != std::string::npos) {
					state = 1;
					printInfo("Found Inproceedings entry");
					paperBuffer.clear();
					paperBuffer.push_back(line);
				} else if (line.find("<dblp:Article") != std::string::npos) {
					state = 3;
					printInfo("Found Article entry");
					paperBuffer.clear();
					paperBuffer.push_back(line);
				}
			} else {
				paperBuffer.push_back(line);
				if (state == 1 && line.find("</dblp:Inproceedings") != std::string::npos) {
					state = 0;
					printInfo("End of Inproceedings entry");
#ifdef USE_THREAD_POOL
					threadPool.enqueue([paperBuffer]() { processPaperBuffer(paperBuffer); });
#else
					processPaperBuffer(paperBuffer);
#endif
				} else if (state == 3 && line.find("</dblp:Article") != std::string::npos) {
					state = 0;
					printInfo("End of Article entry");
#ifdef USE_THREAD_POOL
					threadPool.enqueue([paperBuffer]() { processPaperBuffer(paperBuffer); });
#else
					processPaperBuffer(paperBuffer);
#endif
				}
			}
		}
		std::cout << "Waiting for threads to finish parsing..." << std::endl;
		threadPool.waitForAll();
		std::cout << "saving CSVs..." << std::endl;
		dumpData();
	} catch (const std::exception& e) {
		printError("Exception: " + std::string(e.what()));
		return 1;
	} catch (...) {
		printError("Unknown exception occurred.");
		return 1;
	}
	return 0;
}