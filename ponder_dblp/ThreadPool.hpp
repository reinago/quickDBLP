#pragma once
#include <queue>
#include <mutex>
#include <functional>

class ThreadPool {
public:
	ThreadPool(size_t numThreads) {
		for (size_t i = 0; i < numThreads; ++i) {
			workers.emplace_back([this]() {
				while (true) {
					std::function<void()> task;
					{
						std::unique_lock<std::mutex> lock(queueMutex);
						condition.wait(lock, [this]() { return stop || !tasks.empty(); });
						if (stop && tasks.empty()) return;
						task = std::move(tasks.front());
						tasks.pop();
					}
					task();
				}
				});
		}
	}

	~ThreadPool() {
		{
			std::unique_lock<std::mutex> lock(queueMutex);
			stop = true;
		}
		condition.notify_all();
		for (std::thread& worker : workers) {
			worker.join();
		}
	}

	void enqueue(std::function<void()> task) {
		{
			std::unique_lock<std::mutex> lock(queueMutex);
			tasks.push(std::move(task));
		}
		condition.notify_one();
	}

	void waitForAll() {
		std::unique_lock<std::mutex> lock(queueMutex);
		condition.wait(lock, [this]() { return tasks.empty(); });
	}

private:
	std::vector<std::thread> workers;
	std::queue<std::function<void()>> tasks;
	std::mutex queueMutex;
	std::condition_variable condition;
	bool stop = false;
};