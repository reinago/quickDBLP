#pragma once
#include <atomic>
#include <cstddef>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <tuple>
#include <unordered_map>
#include <vector>

// Hands out dense numeric IDs (1, 2, 3, ...) for strings; the same string always gets the same ID.
// The map is split into shards with their own mutex so that many threads can look up IDs without
// queueing behind one lock; IDs come from one atomic counter. Lookups take a string_view, so no
// temporary std::string is built for keys that already exist.
template <typename IDType, size_t Shards = 64>
class ThreadSafeIDGenerator {
public:
	// Pre-sizes the maps so they never rehash while all threads are waiting for them
	void reserve(size_t expectedTotal) {
		for (auto& shard : shards_) shard.map.reserve(expectedTotal / Shards + 1);
	}

	// Returns {ID, true if the ID was created by this call}
	std::tuple<IDType, bool> getOrCreateID(std::string_view key) {
		Shard& shard = shards_[shardOf(key)];
		std::lock_guard<std::mutex> lock(shard.mutex);
		auto it = shard.map.find(key);
		if (it != shard.map.end()) {
			return { it->second, false };
		}
		IDType newID = nextID_.fetch_add(1, std::memory_order_relaxed);
		shard.map.emplace(std::string(key), newID);
		return { newID, true };
	}

	// Like above, but calls whileLocked(ID, created) before the key's shard is unlocked. Whatever it does with
	// data belonging to this key is therefore atomic with the lookup: no other thread can see the ID without
	// also seeing the data, or update the data at the same time.
	template <typename F>
	IDType getOrCreateID(std::string_view key, F&& whileLocked) {
		Shard& shard = shards_[shardOf(key)];
		std::lock_guard<std::mutex> lock(shard.mutex);
		auto it = shard.map.find(key);
		if (it != shard.map.end()) {
			whileLocked(it->second, false);
			return it->second;
		}
		IDType newID = nextID_.fetch_add(1, std::memory_order_relaxed);
		shard.map.emplace(std::string(key), newID);
		whileLocked(newID, true);
		return newID;
	}

	// The highest ID handed out so far (IDs start at 1; 0 means none)
	IDType getMaxID() const {
		return nextID_.load(std::memory_order_relaxed) - 1;
	}

private:
	struct StringHash {
		using is_transparent = void;
		size_t operator()(std::string_view s) const noexcept { return std::hash<std::string_view>{}(s); }
	};
	struct Shard {
		std::mutex mutex;
		std::unordered_map<std::string, IDType, StringHash, std::equal_to<>> map;
	};

	static size_t shardOf(std::string_view key) {
		// the hash's high bits, so the choice is independent of the bucket index inside the shard's map
		return (std::hash<std::string_view>{}(key) >> 20) % Shards;
	}

	Shard shards_[Shards];
	std::atomic<IDType> nextID_{ static_cast<IDType>(1) };
};
