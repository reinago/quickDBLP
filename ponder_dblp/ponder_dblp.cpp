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
std::unordered_map<std::string, int> papersToNumbers;
std::unordered_map<std::string, int> authorsToNumbers;
int maxPaperNumber = 1;
int maxAuthorNumber = 1;

//std::vector<std::tuple<std::string, std::string, std::string>> currCreators;
//std::vector<std::string> currAuthors;
//std::string currPaperID;
//std::string currTitle;
//int currYear = 0;

// File streams
std::ofstream papersFile("dblp_papers.csv");
std::ofstream authorsFile("dblp_authors.csv");
std::ofstream papersAuthorsFile("dblp_papers_authors.csv");

// Helper functions
int checkAuthor(const std::string& authorID, const std::string& authorOrcid, const std::string& authorName) {
	if (authorsToNumbers.find(authorID) != authorsToNumbers.end()) {
		printInfo("Found author ID: " + authorID);
		return authorsToNumbers[authorID];
	} else {
		printInfo("New author ID: " + authorID);
		int realID = maxAuthorNumber++;
		authorsToNumbers[authorID] = realID;
		printInfo("Assigned number " + std::to_string(realID) + " to author ID " + authorID);
		authorsFile << realID << "\t" << authorID << "\t" << authorName << "\t" << authorOrcid << "\n";
		return realID;
	}
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
				papersToNumbers[currPaperID] = maxPaperNumber++;
				printInfo("Assigned number " + std::to_string(maxPaperNumber - 1) + " to paper ID " + currPaperID);
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
					papersAuthorsFile << maxPaperNumber - 1 << "\t" << realID << "\n";
				}
			}
			papersFile << maxPaperNumber - 1 << "\t" << currPaperID << "\t" << currTitle << "\t" << currYear << "\n";
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

int main() {
	// Initialize files
	papersFile << "NumericID\tDBLP\tTitle\tYear\n";
	authorsFile << "NumericID\tDBLP\tName\tORCID\n";
	papersAuthorsFile << "PaperID\tAuthorID\n";

	try {
		// Open gzipped input file using Boost.Iostreams
		boost::iostreams::filtering_istream inputFile;
		inputFile.push(boost::iostreams::gzip_decompressor());
		inputFile.push(boost::iostreams::file_descriptor("dblp.rdf.gz"));

		if (!inputFile) {
			printError("Could not open input file.");
			return 1;
		}


		// Process file
		std::string line;
		int state = 0; // 0 = searching, 1 = inproceedings, 3 = article
		std::vector<std::string> paperBuffer;
		while (std::getline(inputFile, line)) {
			if (state == 0) {
				if (line.find("<dblp:Inproceedings") != std::string::npos) {
					state = 1;
					printInfo("Found Inproceedings entry");
					paperBuffer.clear();
					paperBuffer.push_back(line);
					// addPaper(line);
				} else if (line.find("<dblp:Article") != std::string::npos) {
					state = 3;
					printInfo("Found Article entry");
					paperBuffer.clear();
					paperBuffer.push_back(line);
					// addPaper(line);
				}
			} else {
				paperBuffer.push_back(line);
				if (state == 1 && line.find("</dblp:Inproceedings") != std::string::npos) {
					state = 0;
					printInfo("End of Inproceedings entry");
					processPaperBuffer(paperBuffer);
					// finalizePaper();
				} else if (state == 3 && line.find("</dblp:Article") != std::string::npos) {
					state = 0;
					printInfo("End of Article entry");
					processPaperBuffer(paperBuffer);
					// finalizePaper();
				}
			}
		}

		// Close files
		papersFile.close();
		authorsFile.close();
		papersAuthorsFile.close();
	} catch (const std::exception& e) {
		printError("Exception: " + std::string(e.what()));
		return 1;
	} catch (...) {
		printError("Unknown exception occurred.");
		return 1;
	}
	return 0;
}