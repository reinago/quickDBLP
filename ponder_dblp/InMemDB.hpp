#pragma once
#include <shared_mutex>

template <typename IDType, typename EntryType>
class InMemDB {
public:
	InMemDB() = default;
	~InMemDB() = default;

	void storeItem(const IDType& id, const EntryType& item) {
		std::unique_lock<std::shared_mutex> writeLock(mutex_); // Lock for writing
		map_[id] = item;
	}

	EntryType getItem(const IDType& id) const {
		std::shared_lock<std::shared_mutex> readLock(mutex_); // Lock for reading
		auto it = map_.find(id);
		if (it != map_.end()) {
			return it->second; // Return the found item
		}
		throw std::runtime_error("Item not found"); // Item not found
	}

private:
	mutable std::shared_mutex mutex_; // Shared mutex for read-write locking
	std::unordered_map<IDType, EntryType> map_; // The underlying map
};

template <typename IDType>
class LinkDB {
public:
	LinkDB() = default;
	~LinkDB() = default;
	void storeLink(const IDType& id1, const IDType& id2) {
		std::unique_lock<std::shared_mutex> writeLock(mutex_); // Lock for writing
		links_.emplace_back(id1, id2);
	}
	// return an iterator over all items
	auto begin() const {
		return links_.begin();
	}
	auto end() const {
		return links_.end();
	}
	size_t size() const {
		std::shared_lock<std::shared_mutex> readLock(mutex_); // Lock for reading
		return links_.size();
	}
	std::pair<IDType, IDType> getItem(size_t index) const {
		std::shared_lock<std::shared_mutex> readLock(mutex_); // Lock for reading
		if (index < links_.size()) {
			return links_[index];
		}
		throw std::out_of_range("Index out of range");
	}

private:
	mutable std::shared_mutex mutex_; // Shared mutex for read-write locking
	std::vector<std::pair<IDType, IDType>> links_; // The underlying map
};