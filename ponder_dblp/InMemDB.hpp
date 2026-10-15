#pragma once
#include <array>
#include <atomic>
#include <cstdint>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <utility>
#include <vector>

// Storage for items with dense numeric IDs (1, 2, 3, ...), written once each by any thread.
// Items live in fixed-size chunks that are allocated on first use, so storing an item needs no global
// lock (different IDs never touch the same slot) and nothing is ever moved or rehashed.
// Reading is only allowed once all writers are done.
template <typename IDType, typename EntryType>
class InMemDB {
public:
	void storeItem(const IDType& id, EntryType&& item) {
		chunkFor(id)[id & kMask] = std::move(item);
	}

	// Modify a stored item (the caller must make sure that no other thread touches the same ID)
	EntryType& item(const IDType& id) {
		return chunkFor(id)[id & kMask];
	}

	// Empty (default constructed) entries are IDs that were handed out but never stored
	const EntryType& getItem(const IDType& id) const {
		if ((id >> kBits) >= kMaxChunks) throw std::runtime_error("Item not found");
		const EntryType* chunk = chunks_[id >> kBits].load(std::memory_order_acquire);
		if (!chunk) throw std::runtime_error("Item not found");
		return chunk[id & kMask];
	}

private:
	static constexpr size_t kBits = 16;
	static constexpr size_t kChunk = size_t(1) << kBits;
	static constexpr size_t kMask = kChunk - 1;
	static constexpr size_t kMaxChunks = 8192;  // 8192 * 65536 = 536M items

	EntryType* chunkFor(IDType id) {
		size_t index = id >> kBits;
		if (index >= kMaxChunks) throw std::length_error("too many items");
		EntryType* chunk = chunks_[index].load(std::memory_order_acquire);
		if (chunk) return chunk;
		std::lock_guard<std::mutex> lock(allocMutex_);
		chunk = chunks_[index].load(std::memory_order_relaxed);
		if (!chunk) {
			owned_.emplace_back(new EntryType[kChunk]);
			chunk = owned_.back().get();
			chunks_[index].store(chunk, std::memory_order_release);
		}
		return chunk;
	}

	std::array<std::atomic<EntryType*>, kMaxChunks> chunks_{};
	std::vector<std::unique_ptr<EntryType[]>> owned_;
	std::mutex allocMutex_;
};

// The (first, second) pairs of a relation. Threads append the links of a whole paper at once.
template <typename IDType>
class LinkDB {
public:
	void reserve(size_t n) { links_.reserve(n); }

	void storeLinks(const IDType& id1, const IDType* ids2, size_t count) {
		std::lock_guard<std::mutex> lock(mutex_);
		for (size_t i = 0; i < count; ++i) links_.emplace_back(id1, ids2[i]);
	}
	auto begin() const { return links_.begin(); }
	auto end() const { return links_.end(); }
	size_t size() const { return links_.size(); }

private:
	std::mutex mutex_;
	std::vector<std::pair<IDType, IDType>> links_;
};
