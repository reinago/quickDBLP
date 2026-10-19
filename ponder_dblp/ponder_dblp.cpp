// Turns the DBLP RDF dump (dblp.rdf.gz) into tab-separated tables of authors, papers and their relations.
//
// How it works: one thread decompresses the dump in large blocks and cuts it into records (one per
// publication), a pool of worker threads parses each record and enters it into the in-memory tables, and at
// the end the tables are written as CSV files into the current directory.
//
// usage: ponder_dblp [path/to/dump.rdf.gz]     (default: dblp.rdf.gz in the current directory)

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <future>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <tuple>
#include <unordered_map>
#include <vector>

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

// The message is only built when the level is enabled (the arguments of a plain printInfo call would be
// concatenated for every paper and author even if nothing is printed).
#define LOG_INFO(message) \
	do { if (printLevel >= LogLevel::Info) printInfo(message); } while (0)

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

// records that could not be processed (malformed); the run continues but reports them at the end
std::atomic<uint64_t> failedRecords{ 0 };

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
		Reference,   // entries of reference works (encyclopedias, handbooks)
		Editorship,  // edited volumes: the people are editors (dblp:editedBy), not authors
		Withdrawn,
		NUMBER_OF_PARSER_STATES
	};

	static std::string_view entityName(Value st) {
		switch (st) {
		case Inproceedings: return "Inproceedings";
		case Incollection: return "Incollection";
		case Article: return "Article";
		case Book: return "Book";
		case Part: return "Part";
		case Informal: return "Informal";
		case Data: return "Data";
		case Reference: return "Reference";
		case Editorship: return "Editorship";
		case Withdrawn: return "Withdrawn";
		default: return "Unknown";
		}
	}

	// Between records: does this line open a record of one of the known types ("<dblp:" + type name at the
	// beginning of the line)? Returns the type, or Searching if not.
	static Value startedEntity(std::string_view line) {
		constexpr std::string_view prefix = "<dblp:";
		if (line.size() <= prefix.size() || line.compare(0, prefix.size(), prefix) != 0) return Searching;
		const std::string_view rest = line.substr(prefix.size());
		for (int i = 1; i < NUMBER_OF_PARSER_STATES; ++i) {
			const auto name = entityName(static_cast<Value>(i));
			if (rest.size() >= name.size() && rest.compare(0, name.size(), name) == 0) return static_cast<Value>(i);
		}
		return Searching;
	}

	// Inside a record of the given type: does this line close it ("</dblp:" + type name at the beginning)?
	static bool endsEntity(std::string_view line, Value st) {
		constexpr std::string_view prefix = "</dblp:";
		const auto name = entityName(st);
		return line.size() >= prefix.size() + name.size() && line.compare(0, prefix.size(), prefix) == 0 &&
			line.compare(prefix.size(), name.size(), name) == 0;
	}
};

std::string dump_node(pugi::xml_node n) {
	std::ostringstream oss;
	n.print(oss);
	return oss.str();
}

// The output files are tab-separated with one record per line, so tabs and line breaks inside
// titles/names (e.g. arXiv titles with math) must not reach them. Runs are collapsed to one space.
std::string sanitizeField(std::string_view s) {
	std::string out;
	out.reserve(s.size());
	bool pendingSpace = false;
	for (char c : s) {
		if (c == '\t' || c == '\r' || c == '\n') {
			pendingSpace = !out.empty();
		} else {
			if (pendingSpace) out += ' ';
			pendingSpace = false;
			out += c;
		}
	}
	return out;
}

pugi::xml_node check_child(pugi::xml_node n, const char* name) {
	auto c = n.child(name);
	if (c == nullptr) throw std::invalid_argument(std::string("could not find '") + name + "' in '" + n.name() + "'\n" + dump_node(n));
	return c;
}

// rdf:resource of a node, or "" if it has none
std::string_view check_resource(pugi::xml_node n) {
	return n.attribute("rdf:resource").value();
}

std::string_view firstLine(const std::string& text) {
	return std::string_view(text).substr(0, text.find('\n'));
}

// The same person is signed differently on different papers (with or without ORCID, "Ruben D. Vromans" or
// "Ruben Vromans"). Which signature is seen first depends on thread timing, so a fixed rule picks one:
// any ORCID wins, and of several name variants the smallest string (this also prefers DBLP's "... 0001" form).
void mergeAuthor(Author& author, std::string_view orcid, std::string_view name) {
	if (!orcid.empty() && (author.orcid.empty() || orcid < author.orcid)) {
		author.orcid = orcid;
	}
	if (name != std::string_view(author.name)) {
		std::string candidate = sanitizeField(name);
		if (!candidate.empty() && (author.name.empty() || candidate < author.name)) {
			author.name = std::move(candidate);
		}
	}
}

// Parses one record (the text of a <dblp:...> element, modified in place by the XML parser) and enters it
// into the tables. Records that do not qualify are skipped (see the rules below).
void processPaperBuffer(std::string& record, ParserState::Value paperType) {
	pugi::xml_document doc;
	auto result = doc.load_buffer_inplace(record.data(), record.size());
	if (result.status != pugi::status_ok) {
		throw std::invalid_argument("could not parse publication: " + std::string(firstLine(record)));
	}

	auto pub = doc.first_child();
	if (pub.empty()) {
		throw std::invalid_argument("empty publication: " + std::string(firstLine(record)));
	}
	const std::string_view currPaperID = pub.attribute("rdf:about").value();
	if (currPaperID.empty()) {
		printWarning("No ID found, skipping:\n" + dump_node(pub));
		return;
	}
	LOG_INFO("Current ID: " + std::string(currPaperID));

	// the people of the record: authors, or editors in Editorship records
	size_t numPeople = 0;
	for (const char* personTag : { "dblp:authoredBy", "dblp:editedBy" }) {
		for (pugi::xml_node person : pub.children(personTag)) {
			(void)person;
			++numPeople;
		}
	}

	struct Creator {
		std::string_view id, orcid, name;
	};
	thread_local std::vector<Creator> creators;
	creators.clear();
	for (pugi::xml_node sig : pub.children("dblp:hasSignature")) {
		for (pugi::xml_node sigContent : sig.children()) {
			const std::string_view sigName = sigContent.name();
			if (sigName != "dblp:AuthorSignature" && sigName != "dblp:EditorSignature") continue;
			Creator c;
			c.id = check_resource(check_child(sigContent, "dblp:signatureCreator"));
			c.orcid = check_resource(sigContent.child("dblp:signatureOrcid"));
			c.name = check_child(sigContent, "dblp:signatureDblpName").child_value();
			LOG_INFO("Found creator: " + std::string(c.id) + ", " + std::string(c.orcid) + ", " + std::string(c.name));
			creators.push_back(c);
		}
	}

	// Rules: the record is skipped without authors, with differing numbers of people and signatures, without
	// a title, or without any year
	if (numPeople != creators.size()) {
		printError("Number of authors and creators do not match for paper " + std::string(currPaperID) + ": " +
			std::to_string(numPeople) + " vs " + std::to_string(creators.size()));
		return;
	}
	if (numPeople == 0) {
		LOG_INFO("No authors found for paper " + std::string(currPaperID) + ", skipping");
		return;
	}
	auto child_title = pub.child("dblp:title");
	if (child_title == nullptr) {
		printWarning("No title found for paper " + std::string(currPaperID) + ", skipping\n" + dump_node(pub));
		return;
	}

	auto child_year = pub.child("dblp:yearOfPublication");
	if (child_year == nullptr) {
		child_year = pub.child("dblp:yearOfEvent");
		if (child_year == nullptr) {
			printWarning("No year found for paper " + std::string(currPaperID) + ", skipping\n" + dump_node(pub));
			return;
		}
	}
	const uint16_t currYear = static_cast<uint16_t>(std::stoi(child_year.child_value()));

	const uint32_t paperID = std::get<0>(papersToNumbers.getOrCreateID(currPaperID));
	LOG_INFO("Assigned number " + std::to_string(paperID) + " to paper ID " + std::string(currPaperID));

	thread_local std::vector<uint32_t> authorIDs;
	authorIDs.clear();
	for (const Creator& c : creators) {
		const uint32_t authorID = authorsToNumbers.getOrCreateID(c.id, [&](uint32_t id, bool isNew) {
			if (isNew) {
				authorDB.storeItem(id, Author{ std::string(c.id), std::string(c.orcid), sanitizeField(c.name) });
			} else {
				mergeAuthor(authorDB.item(id), c.orcid, c.name);
			}
		});
		authorIDs.push_back(authorID);
	}
	// DBLP sometimes lists the same person twice on a paper: store one link only
	std::sort(authorIDs.begin(), authorIDs.end());
	authorIDs.erase(std::unique(authorIDs.begin(), authorIDs.end()), authorIDs.end());
	papersAndAuthorsDB.storeLinks(paperID, authorIDs.data(), authorIDs.size());

	paperDB.storeItem(paperID, Paper{ std::string(currPaperID), sanitizeField(child_title.child_value()),
		static_cast<uint8_t>(paperType), currYear });
}

// Runs in a worker thread: a malformed record is counted and reported, but does not stop the run
void processRecordSafely(std::string& record, ParserState::Value paperType) {
	try {
		processPaperBuffer(record, paperType);
	} catch (const std::exception& e) {
		++failedRecords;
		printError(std::string("record skipped: ") + e.what());
	}
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

		lastProgress = current;
	}
}

// Writes into a large buffer and hands it to the file in big pieces (a plain ofstream issues one small
// write per few KB, which is very slow with virus scanners and network drives)
class BufferedFile {
public:
	explicit BufferedFile(const std::string& path) : file(std::fopen(path.c_str(), "wb")) {
		if (!file) throw std::runtime_error("Failed to create " + path);
		buffer.reserve(kFlushSize + 4096);
	}
	~BufferedFile() { close(); }
	BufferedFile(const BufferedFile&) = delete;
	BufferedFile& operator=(const BufferedFile&) = delete;

	BufferedFile& operator<<(std::string_view s) { buffer.append(s); flushIfFull(); return *this; }
	BufferedFile& operator<<(char c) { buffer.push_back(c); return *this; }
	BufferedFile& operator<<(uint64_t v) {
		char tmp[24];
		auto r = std::to_chars(tmp, tmp + sizeof(tmp), v);
		buffer.append(tmp, r.ptr - tmp);
		return *this;
	}
	void close() {
		if (!file) return;
		flush();
		std::fclose(file);
		file = nullptr;
	}

private:
	static constexpr size_t kFlushSize = size_t(8) << 20;
	void flushIfFull() { if (buffer.size() >= kFlushSize) flush(); }
	void flush() {
		if (!buffer.empty() && std::fwrite(buffer.data(), 1, buffer.size(), file) != buffer.size()) {
			throw std::runtime_error("Failed to write output (disk full?)");
		}
		buffer.clear();
	}
	std::FILE* file;
	std::string buffer;
};

// text for use inside a JSON string (Windows paths contain backslashes)
std::string jsonEscape(const std::string& s) {
	std::string out;
	for (char c : s) {
		if (c == '\\' || c == '"') out += '\\';
		if (static_cast<unsigned char>(c) < 0x20) continue;
		out += c;
	}
	return out;
}

void dumpData(const std::string& inputFilePath) {
	uint64_t papersWritten = 0, authorsWritten = 0;
	std::atomic<uint64_t> missing{ 0 };

	// The three files are independent, so they are written at the same time
	auto dumpPapers = [&]() {
		BufferedFile papersFile("dblp_papers.csv");
		papersFile << "NumericID\tDBLP\tTitle\tType\tYear\n";
		const uint32_t maxPaper = papersToNumbers.getMaxID();
		for (uint32_t p = 1; p <= maxPaper; ++p) {
			const Paper& paper = paperDB.getItem(p);
			if (paper.id.empty()) { ++missing; continue; }  // an ID that was handed out but never stored
			papersFile << uint64_t(p) << '\t' << paper.id << '\t' << paper.title << '\t' << uint64_t(paper.type) << '\t'
				<< uint64_t(paper.year) << '\n';
			++papersWritten;
		}
	};
	auto dumpAuthors = [&]() {
		BufferedFile authorsFile("dblp_authors.csv");
		authorsFile << "NumericID\tDBLP\tName\tORCID\n";
		const uint32_t maxAuthor = authorsToNumbers.getMaxID();
		for (uint32_t a = 1; a <= maxAuthor; ++a) {
			const Author& author = authorDB.getItem(a);
			if (author.id.empty()) { ++missing; continue; }
			authorsFile << uint64_t(a) << '\t' << author.id << '\t' << author.name << '\t' << author.orcid << '\n';
			++authorsWritten;
		}
	};
	auto dumpLinks = [&]() {
		BufferedFile papersAuthorsFile("dblp_papers_authors.csv");
		papersAuthorsFile << "PaperID\tAuthorID\n";
		for (const auto& link : papersAndAuthorsDB) {
			papersAuthorsFile << uint64_t(link.first) << '\t' << uint64_t(link.second) << '\n';
		}
	};
	std::cout << "Dumping papers, authors and relations..." << std::endl;
	auto authorsTask = std::async(std::launch::async, dumpAuthors);
	auto linksTask = std::async(std::launch::async, dumpLinks);
	dumpPapers();
	authorsTask.get();  // also passes on exceptions of the other threads
	linksTask.get();
	std::cout << "Dumping completed." << std::endl;
	if (missing) {
		printError(std::to_string(missing) + " IDs were handed out but their records were never stored");
	}

	// get datetime of rdf file
	auto lwt = std::filesystem::last_write_time(inputFilePath);
	std::ofstream metadataFile("dblp_metadata.json");
	metadataFile << "{\n";
	metadataFile << "  \"source_file\": \"" << jsonEscape(inputFilePath) << "\",\n";
	metadataFile << "  \"source_file_last_write_time\": \"" << lwt << "\",\n";
	metadataFile << "  \"total_papers\": " << papersWritten << ",\n";
	metadataFile << "  \"total_authors\": " << authorsWritten << ",\n";
	metadataFile << "  \"total_links\": " << papersAndAuthorsDB.size() << ",\n";
	metadataFile << "  \"records_failed\": " << failedRecords.load() << "\n";
	metadataFile << "}\n";
}

// Reads the dump in large blocks and hands every publication record to the worker pool
void readDump(const std::string& inputFilePath, ThreadPool& threadPool) {
	gzFile file = zng_gzopen(inputFilePath.c_str(), "rb");
	if (!file) {
		throw std::runtime_error("Failed to open file with zlib: " + inputFilePath);
	}
	zng_gzbuffer(file, 1 << 20);
	const uint64_t compressedSize = std::filesystem::file_size(inputFilePath);

	constexpr size_t kMaxRecord = size_t(256) << 20;
	std::vector<char> block(size_t(32) << 20);
	std::string carry;   // the incomplete last line of the previous block
	std::string record;  // the record being collected, line by line
	record.reserve(1 << 14);
	ParserState::Value state = ParserState::Searching;

	// Records go to the workers in batches: waking a worker thread for every single record costs more than parsing
	// it, especially on Linux and in virtual machines (WSL), where every wake-up is a system call and often an
	// inter-processor interrupt.
	struct Record {
		std::string text;
		ParserState::Value type;
	};
	constexpr size_t kBatchRecords = 64;
	std::vector<Record> batch;
	batch.reserve(kBatchRecords);
	auto flushBatch = [&]() {
		if (batch.empty()) return;
		auto work = std::make_shared<std::vector<Record>>(std::move(batch));
		batch = std::vector<Record>();
		batch.reserve(kBatchRecords);
		threadPool.enqueue([work]() {
			for (Record& r : *work) processRecordSafely(r.text, r.type);
		});
	};

	auto handleLine = [&](std::string_view line) {
		if (state == ParserState::Searching) {
			state = ParserState::startedEntity(line);
			if (state != ParserState::Searching) {
				record.clear();
				record.append(line);
				record.push_back('\n');
			}
			return;
		}
		record.append(line);
		record.push_back('\n');
		if (ParserState::endsEntity(line, state)) {
			batch.push_back({ std::move(record), state });
			if (batch.size() >= kBatchRecords) flushBatch();
			record = std::string();
			record.reserve(1 << 14);
			state = ParserState::Searching;
		} else if (record.size() > kMaxRecord) {
			throw std::runtime_error("record larger than 256 MB (malformed dump?): " + std::string(firstLine(record)));
		}
	};

	int numRead = 0;
	while ((numRead = zng_gzread(file, block.data(), static_cast<unsigned>(block.size()))) > 0) {
		const char* p = block.data();
		const char* const end = p + numRead;
		while (p < end) {
			const char* newline = static_cast<const char*>(std::memchr(p, '\n', end - p));
			if (!newline) {
				carry.append(p, end - p);
				break;
			}
			if (carry.empty()) {
				handleLine(std::string_view(p, newline - p));
			} else {
				carry.append(p, newline - p);
				handleLine(carry);
				carry.clear();
			}
			p = newline + 1;
		}
		const auto offset = zng_gzoffset(file);
		if (offset > 0) checkProgress(static_cast<uint64_t>(offset), compressedSize);
	}
	if (numRead < 0) {
		int errnum = 0;
		const char* message = zng_gzerror(file, &errnum);
		throw std::runtime_error(std::string("Error while reading ") + inputFilePath + ": " + (message ? message : "unknown"));
	}
	if (!carry.empty()) handleLine(carry);  // last line without line break
	flushBatch();
	zng_gzclose(file);
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

int main(int argc, char** argv) {
	const std::string inputFilePath = argc > 1 ? argv[1] : "dblp.rdf.gz";
	const std::string lockFilePath = inputFilePath + ".lock";

	try {
		if (!checkLockFile(lockFilePath)) {
			return 1;
		}

		{
			// Size the tables for the input (about 9M papers, 4.5M authors and 31M links per 2.7 GB of dump) so
			// that nothing has to be rehashed or reallocated while all threads wait for it.
			const uint64_t compressedSize = std::filesystem::exists(inputFilePath) ? std::filesystem::file_size(inputFilePath) : 0;
			papersToNumbers.reserve(std::max<uint64_t>(1000, compressedSize / 300));
			authorsToNumbers.reserve(std::max<uint64_t>(1000, compressedSize / 600));
			papersAndAuthorsDB.reserve(std::max<uint64_t>(1000, compressedSize / 85));
		}

		{
			Timer timer("Processing database dump...");
			// One thread reads and cuts up the dump; the other cores parse. More threads than cores only add
			// lock contention.
			const size_t workers = std::max(2u, std::thread::hardware_concurrency());
			ThreadPool threadPool(workers, 8 * workers);  // each queued task is a batch of records
			readDump(inputFilePath, threadPool);
			std::cout << std::endl << "Waiting for threads to finish parsing... ";
			threadPool.waitForAll();
			std::cout << "Done." << std::endl;
		}
		{
			Timer timer("Saving CSVs...");
			dumpData(inputFilePath);
		}
		if (failedRecords > 0) {
			printError(std::to_string(failedRecords.load()) + " malformed records were skipped; the output is incomplete");
			removeLockFile(lockFilePath);
			std::cout.flush();
			std::quick_exit(2);
		}
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
	// Everything is written and closed. Skip the destructors of the big tables: freeing millions of small
	// strings would only delay the end of the program by several seconds.
	std::cout.flush();
	std::quick_exit(0);
}
