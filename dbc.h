/*
Safety-check constants and helper
----------------------------------

`C_STACK_DEPTH` controls how many call frames are printed on a REQUIRES,
ENSURES, or ASSERT failure. The `safety_abort` function is called exclusively
from those three macros; it must never be called directly.
*/

    #define C_STACK_DEPTH   16U  // Maximum number of stack frames to print on abort

    // Forward declaration required so the macros below can reference it.
    void safety_abort(const char* check_type,
                      const char* func_name,
                      uint32_t    line_num,
                      const char* cond_str,
                      const char* format, ...);

    // REQUIRES: asserts a precondition; aborts with a message if the condition is false.
    // The bug is in the caller when this fires.
    #define REQUIRES(condition, ...) \
        if(false == (condition)) \
        { \
            safety_abort("REQUIRES", __func__, __LINE__, #condition, __VA_ARGS__); \
        }

    // ENSURES: asserts a postcondition; aborts with a message if the condition is false.
    // The bug is in the function itself when this fires.
    #define ENSURES(condition, ...) \
        if(false == (condition)) \
        { \
            safety_abort("ENSURES", __func__, __LINE__, #condition, __VA_ARGS__); \
        }

    // ASSERT: asserts a mid-function invariant; aborts with a message if the condition is false.
    #define ASSERT(condition, ...) \
        if(false == (condition)) \
        { \
            safety_abort("ASSERT", __func__, __LINE__, #condition, __VA_ARGS__); \
        }
