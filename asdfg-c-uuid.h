    #include <stdbool.h>        // bool

    // RFC 4122 UUID
    #define C_UUID_STRLEN               36U         // Character count of a UUID string (no null)
    #define C_UUID_LEN                  37U         // Buffer size including null terminator

    bool is_valid_uuid(const char* uuid_value);
    int32_t generate_random_uuid(char* uuid_value);
