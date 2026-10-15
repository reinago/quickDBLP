#pragma once
#include <condition_variable>
#include <functional>
#include <mutex>
#include <queue>
#include <thread>
#include <vector>

// A fixed set of worker threads with a bounded task queue: when the queue is full, enqueue() waits, so a
// fast producer cannot run far ahead of the workers and pile up unprocessed input in memory.
class ThreadPool {
public:
	explicit ThreadPool(size_t numThreads, size_t maxQueued = 4096) : maxQueued(maxQueued) {
		for (size_t i = 0; i < numThreads; ++i) {
			workers.emplace_back([this]() {
				while (true) {
					std::function<void()> task;
					{
						std::unique_lock<std::mutex> lock(queueMutex);
						notEmpty.wait(lock, [this]() { return stop || !tasks.empty(); });
						if (stop && tasks.empty()) return;
						task = std::move(tasks.front());
						tasks.pop();
						++activeTasks;
					}
					notFull.notify_one();
					task();
					{
						std::unique_lock<std::mutex> lock(queueMutex);
						--activeTasks;
						if (tasks.empty() && activeTasks == 0) allDone.notify_all();
					}
				}
			});
		}
	}

	~ThreadPool() {
		{
			std::unique_lock<std::mutex> lock(queueMutex);
			stop = true;
		}
		notEmpty.notify_all();
		for (std::thread& worker : workers) {
			worker.join();
		}
	}

	void enqueue(std::function<void()> task) {
		{
			std::unique_lock<std::mutex> lock(queueMutex);
			notFull.wait(lock, [this]() { return tasks.size() < maxQueued; });
			tasks.push(std::move(task));
		}
		notEmpty.notify_one();
	}

	void waitForAll() {
		std::unique_lock<std::mutex> lock(queueMutex);
		allDone.wait(lock, [this]() { return tasks.empty() && activeTasks == 0; });
	}

private:
	std::vector<std::thread> workers;
	std::queue<std::function<void()>> tasks;
	std::mutex queueMutex;
	std::condition_variable notEmpty, notFull, allDone;
	size_t maxQueued;
	bool stop = false;
	size_t activeTasks = 0;
};
