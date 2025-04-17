#pragma once
#include <shared_mutex>

template <typename IDType>
class ThreadSafeIDGenerator {
public:
	// Generates or retrieves the ID for the given string, creating it if necessary
	// Returns a tuple containing the ID and a boolean indicating if it was created
	std::tuple<IDType, bool> getOrCreateID(const std::string& key) {
		// First, check without locking
		{
			std::shared_lock<std::shared_mutex> readLock(mutex_);
			auto it = map_.find(key);
			if (it != map_.end()) {
				return { it->second, false }; // Return the existing ID
			}
		}

		// If not found, lock for writing and check again
		{
			std::unique_lock<std::shared_mutex> writeLock(mutex_);
			auto it = map_.find(key);
			if (it != map_.end()) {
				return { it->second, false }; // Return the existing ID
			}

			// Generate a new ID for the key
			IDType newID = nextID_++;
			map_[key] = newID;
			return { newID, true };
		}
	}

	// Retrieves the ID for the given string
	// Returns a tuple containing the ID and a boolean indicating if it was found
	std::tuple<IDType, bool> getID(const std::string& key) const {
		std::shared_lock<std::shared_mutex> readLock(mutex_); // Lock for reading
		auto it = map_.find(key);
		if (it != map_.end()) {
			return { it->second, true };
		}
		return { IDType(), false };
	}

	IDType getMaxID() const {
		std::shared_lock<std::shared_mutex> readLock(mutex_); // Lock for reading
		return nextID_ - 1; // Return the maximum ID
	}

	// Clears all stored data
	void clear() {
		std::unique_lock<std::shared_mutex> writeLock(mutex_); // Lock for writing
		map_.clear();
		nextID_ = static_cast<IDType>(1);
	}

private:
	mutable std::shared_mutex mutex_; // Shared mutex for read-write locking
	std::unordered_map<std::string, IDType> map_; // The underlying map
	IDType nextID_ = static_cast<IDType>(1); // The next ID to assign
};