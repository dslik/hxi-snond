    // ISO 8601 timestamp
    #define C_ISO8601_LEN               28U         // Buffer size for "YYYY-MM-DDTHH:MM:SS.ffffffZ\0"

    char* iso8601_time(char* buf, size_t len);
