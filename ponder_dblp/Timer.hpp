// a RAII class for measuring elapsed time around a scope.
#pragma once
#include <string>
#include <chrono>
class Timer {
	public:
	Timer(std::string desc) : start_(std::chrono::high_resolution_clock::now()), desc_(desc) {
		std::cout << desc << std::endl;
	}
	~Timer() {
		auto end = std::chrono::high_resolution_clock::now();
		auto elapsed_ = std::chrono::duration_cast<std::chrono::milliseconds>(end - start_).count();
		//std::cout << "Elapsed time (" << desc_ << "): " << elapsed_ << " ms" << std::endl;
		std::cout << "Elapsed time: " << elapsed_ << " ms" << std::endl;
	}
private:
	std::chrono::high_resolution_clock::time_point start_;
	std::string desc_;
};