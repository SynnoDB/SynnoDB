#pragma once

// Increment file version to invalidate cache when this file is changed. This is needed because this file is included in the generated code and changes to it should trigger regeneration of all code that includes it.
// FILE_VERSION: 2

#include <iomanip>
#include <string>
#include <sstream>
#include <vector>

struct QueryRequest {
    std::string query_id; // query-id 
    std::string req_id; // id of the request
    std::string line; // line with query arguments
};

// Helper function to parse IN list from tuple syntax: ('val1', 'val2', ...)
// Necessary e.g. for CEB where some arguments are lists of values
inline std::vector<std::string> parse_in_list(std::istringstream& iss) {
    std::vector<std::string> result;

    // Read opening parenthesis
    char c;
    iss >> std::ws >> c;
    if (c != '(') {
        std::ostringstream oss;
        oss << "Expected '(' at start of IN list, but got '"
            << c << "' (int=" << static_cast<int>(static_cast<unsigned char>(c)) << ")";
        throw std::runtime_error(oss.str());
    }

    bool first = true;
    while (iss >> std::ws) {
        // Check for closing parenthesis
        if (iss.peek() == ')') {
            iss.get(); // consume ')'
            break;
        }

        // Skip comma after first element
        if (!first) {
            iss >> std::ws >> c;
            if (c != ',') {
                throw std::runtime_error("Expected ',' between IN list elements");
            }
        }
        first = false;

        std::string value;
        iss >> std::ws;
        if (iss.peek() == '\'') {
            iss.get();
            while (iss) {
                const char ch = static_cast<char>(iss.get());
                if (!iss) break;
                if (ch == '\'') {
                    if (iss.peek() == '\'') {
                        iss.get();
                        value.push_back('\'');
                        continue;
                    }
                    break;
                }
                value.push_back(ch);
            }
        } else {
            while (iss && iss.peek() != ',' && iss.peek() != ')') {
                value.push_back(static_cast<char>(iss.get()));
            }
            const auto start = value.find_first_not_of(" \t\r\n");
            const auto end = value.find_last_not_of(" \t\r\n");
            if (start == std::string::npos) {
                value.clear();
            } else {
                value = value.substr(start, end - start + 1);
            }
        }

        result.push_back(value);
    }

    return result;
}


//Q1
struct Q1Args {
    std::string DELTA;
};

inline Q1Args parse_q1(const QueryRequest& request) {
    Q1Args args;
    std::istringstream iss(request.line);

	if (!(iss >> std::quoted(args.DELTA))) {
		throw std::runtime_error("Q1: failed to parse DELTA");
	}

    return args;
}

//Q2
struct Q2Args {
    std::string DATE;
};

inline Q2Args parse_q2(const QueryRequest& request) {
    Q2Args args;
    std::istringstream iss(request.line);

	if (!(iss >> std::quoted(args.DATE))) {
		throw std::runtime_error("Q2: failed to parse DATE");
	}

    return args;
}

//Q3
struct Q3Args {
    std::string SEGMENT;
};

inline Q3Args parse_q3(const QueryRequest& request) {
    Q3Args args;
    std::istringstream iss(request.line);

	if (!(iss >> std::quoted(args.SEGMENT))) {
		throw std::runtime_error("Q3: failed to parse SEGMENT");
	}

    return args;
}

//Q4
struct Q4Args {
    std::string BRAND;
    std::string CONTAINER;
};

inline Q4Args parse_q4(const QueryRequest& request) {
    Q4Args args;
    std::istringstream iss(request.line);

	if (!(iss >> std::quoted(args.BRAND))) {
		throw std::runtime_error("Q4: failed to parse BRAND");
	}
	if (!(iss >> std::quoted(args.CONTAINER))) {
		throw std::runtime_error("Q4: failed to parse CONTAINER");
	}

    return args;
}

//Q5
struct Q5Args {
    std::string WORD1;
};

inline Q5Args parse_q5(const QueryRequest& request) {
    Q5Args args;
    std::istringstream iss(request.line);

	if (!(iss >> std::quoted(args.WORD1))) {
		throw std::runtime_error("Q5: failed to parse WORD1");
	}

    return args;
}

//Q6
struct Q6Args {
    std::string SHIPMODE1;
    std::string SHIPMODE2;
    std::string DATE;
};

inline Q6Args parse_q6(const QueryRequest& request) {
    Q6Args args;
    std::istringstream iss(request.line);

	if (!(iss >> std::quoted(args.SHIPMODE1))) {
		throw std::runtime_error("Q6: failed to parse SHIPMODE1");
	}
	if (!(iss >> std::quoted(args.SHIPMODE2))) {
		throw std::runtime_error("Q6: failed to parse SHIPMODE2");
	}
	if (!(iss >> std::quoted(args.DATE))) {
		throw std::runtime_error("Q6: failed to parse DATE");
	}

    return args;
}

//Q7
struct Q7Args {
    std::string COLOR;
    std::string TYPE;
};

inline Q7Args parse_q7(const QueryRequest& request) {
    Q7Args args;
    std::istringstream iss(request.line);

	if (!(iss >> std::quoted(args.COLOR))) {
		throw std::runtime_error("Q7: failed to parse COLOR");
	}
	if (!(iss >> std::quoted(args.TYPE))) {
		throw std::runtime_error("Q7: failed to parse TYPE");
	}

    return args;
}

//Q8
struct Q8Args {
    std::string I1;
    std::string I2;
    std::string I3;
};

inline Q8Args parse_q8(const QueryRequest& request) {
    Q8Args args;
    std::istringstream iss(request.line);

	if (!(iss >> std::quoted(args.I1))) {
		throw std::runtime_error("Q8: failed to parse I1");
	}
	if (!(iss >> std::quoted(args.I2))) {
		throw std::runtime_error("Q8: failed to parse I2");
	}
	if (!(iss >> std::quoted(args.I3))) {
		throw std::runtime_error("Q8: failed to parse I3");
	}

    return args;
}

