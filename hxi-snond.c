// ---------------------------------------------------------------------------------
// HXi Power Supply Sensor Daemon
// ---------------------------------------------------------------------------------
// Polls and writes out sensor data from HXi power supplies.
// Sensor data is written in SNON 4 format.
// ---------------------------------------------------------------------------------
// SPDX-FileCopyrightText: Copyright 2026 David Slik
// SPDX-FileAttributionText: https://github.com/dslik/hxi-snond/
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------------------
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <ctype.h>
#include <limits.h>
#include <string.h>
#include <unistd.h>
#include <time.h>
#include <getopt.h>
#include <fcntl.h>
#include <signal.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/stat.h>

// Linux includes
#include <linux/hidraw.h>

// Local includes
#include "cJSON.h"

// Defines
#define ISO8601_LEN     28

// Prototypes
cJSON* json_read_file(char* json_file_path);
void snon_addname(cJSON* snon_object, char* field, char* lang, char* name);
int hxi_collect_values(char* snon_path, int dev_fd, char* uuid_value);
int hxi_read_power(int dev_fd, uint8_t rail, double* wattage);
int hxi_select_rail(int dev_fd, uint8_t rail);
double reg_to_value(uint16_t reg);
bool is_valid_uuid(char* uuid_value);
int generate_random_uuid(char* uuid_value);
char* iso8601_time(char* buf, size_t len);
static void signal_handler(int signum);


// Global Variables
const char* usage = "Usage: hxi-snond [options]\n"
                    "\n"
                    "Options:\n"
                    "  -d DEVICE   Hidraw device path (default: /dev/hidraw0)\n"
                    "  -o OUTPUT   Location to write SNON files (default: current working directory)\n"
                    "  -c CONFIG   JSON Configuration file that specifies which sensors to read\n"
                    "  -u UUID     Device UUID (default: randomly generated)\n"
                    "  -h          Show this help message\n";

static volatile sig_atomic_t abort_collect = false;

int main(int argc, char* argv[])
{
    int                     opt = 0;
    const char*             device_name = "/dev/hidraw0";
    const char*             output_path = "./";
    const char*             config_path = "./sensors.json";
    const char*             uuid_arg = NULL;
    char                    device_uuid[37];
    char                    sensor_uuid[37];
    int                     dev_fd = 0;
    char                    snon_path[PATH_MAX];
    int                     snon_fd = 0;
    struct hidraw_devinfo   hid_info;
    cJSON*                  config_json = NULL;
    cJSON*                  snon_root = NULL;
    cJSON*                  relation_object = NULL;
    cJSON*                  relation_array = NULL;
    char*                   snon_output = NULL;
    char                    working_string[1024];

    // Obtain command line arguments
    while(-1 != opt)
    {
        opt = getopt(argc, argv, "d:o:c:u:h");

        switch(opt)
        {
            case 'd':
                device_name = optarg;
                break;
            case 'o':
                output_path = optarg;
                break;
            case 'c':
                config_path = optarg;
                break;
            case 'u':
                uuid_arg = optarg;
                break;
            case 'h':
                fprintf(stdout, "%s", usage);
                return EXIT_SUCCESS;
            case '?':
                fprintf(stderr, "%s", usage);
                return EXIT_FAILURE;
        }
    }

    // ===========================================================
    // Validate Inputs
    if(0 != access(device_name, R_OK | W_OK))
    {
        fprintf(stderr,"Error accessing specified hid device: %s\n", device_name);
        perror(device_name);
        return EXIT_FAILURE;
    }

    if(0 != access(output_path, R_OK | W_OK))
    {
        fprintf(stderr,"Error accessing specified output directory: %s\n", output_path);
        perror(output_path);
        return EXIT_FAILURE;
    }

    if(0 != access(config_path, R_OK))
    {
        fprintf(stderr,"Error accessing specified config file: %s\n", config_path);
        perror(config_path);
        return EXIT_FAILURE;
    }

    // Ensure that the output directory ends with a "/"
    if('/' != output_path[strlen(output_path) - 1])
    {
        fprintf(stderr,"Specified output directory does not end with a '/': %s\n", output_path);
        return EXIT_FAILURE;
    }

    // Validate or generate UUID
    if(NULL != uuid_arg)
    {
        if(false == is_valid_uuid((char*) uuid_arg))
        {
            fprintf(stderr,"Invalid UUID format: %s\n", uuid_arg);
            return EXIT_FAILURE;
        }

        memcpy(device_uuid, uuid_arg, sizeof(device_uuid));
    }
    else
    {
        if(-1 == generate_random_uuid(device_uuid))
        {
            fprintf(stderr,"Error generating random UUID\n");
            return EXIT_FAILURE;
        }
    }

    // ===========================================================
    // Initialize a connecion to the HXi Power Supply
    dev_fd = open(device_name, O_RDWR);

    memset(&hid_info, 0, sizeof(hid_info));
    if(0 != ioctl(dev_fd, HIDIOCGRAWINFO, &hid_info))
    {
        fprintf(stderr,"Specified path '%s' is not a HID device.\n", device_name);
        perror(device_name);
        return EXIT_FAILURE;
    }
      
    if(0x1b1c != hid_info.vendor)
    {
        fprintf(stderr,"Unexpected HID vendor '0x%04x' (expected 0x1b1c)\n", hid_info.vendor);
        return EXIT_FAILURE;
    }

    if(0x1c1e != hid_info.product)
    {
        fprintf(stderr,"Unexpected HID product '0x%04x' (expected 0x1c1e)\n", hid_info.product);
        return EXIT_FAILURE;
    }

    // ===========================================================
    // Read a configuration JSON file that specifies which sensors
    // should be collected
    config_json = json_read_file((char*) config_path);

  


    // ===========================================================
    // Create SNON device and sensor records if they do not exist
    snprintf(snon_path, sizeof(snon_path), "%s%s.json", output_path, device_uuid);

    if(0 != access(snon_path, R_OK | W_OK))
    {
        // Create a new SNON device object
        snon_fd = open(snon_path, O_RDWR | O_CREAT | O_TRUNC, S_IRUSR | S_IWUSR);

        // Create a new device record
        snon_root = cJSON_CreateObject();
        snprintf(working_string, sizeof(working_string), "urn:uuid:%s", device_uuid);
        cJSON_AddItemToObjectCS(snon_root, "eID", cJSON_CreateString(working_string));
        cJSON_AddItemToObjectCS(snon_root, "eC", cJSON_CreateString("device"));
        snon_addname(snon_root, "eN", "*", "HX1000i Power Supply");
        snon_output = cJSON_PrintUnformatted(snon_root);

        if(strlen(snon_output) != write(snon_fd, snon_output, strlen(snon_output)))
        {
            fprintf(stderr,"Error writing SNON device record\n");
            perror("write");
            return EXIT_FAILURE;
        }

        cJSON_free(snon_output);
        cJSON_Delete(snon_root);
        close(snon_fd);

        // Create a new SNON sensor object
        if(-1 == generate_random_uuid(sensor_uuid))
        {
            fprintf(stderr,"Error generating random UUID\n");
            return EXIT_FAILURE;
        }

        snprintf(snon_path, sizeof(snon_path), "%s%s.json", output_path, sensor_uuid);
        snon_fd = open(snon_path, O_RDWR | O_CREAT | O_TRUNC, S_IRUSR | S_IWUSR);

        // Create a new sensor record
        snon_root = cJSON_CreateObject();
        snprintf(working_string, sizeof(working_string), "urn:uuid:%s", sensor_uuid);
        cJSON_AddItemToObjectCS(snon_root, "eID", cJSON_CreateString(working_string));
        cJSON_AddItemToObjectCS(snon_root, "eC", cJSON_CreateString("sensor"));
        snon_addname(snon_root, "eN", "*", "3.3V Rail Power");

        relation_array = cJSON_CreateArray();
        snprintf(working_string, sizeof(working_string), "urn:uuid:%s", device_uuid);
        cJSON_AddItemToArray(relation_array, cJSON_CreateString(working_string));

        relation_object = cJSON_CreateObject();
        cJSON_AddItemToObjectCS(relation_object, "child_of", relation_array);
        cJSON_AddItemToObjectCS(snon_root, "eR", relation_object);
        snon_output = cJSON_PrintUnformatted(snon_root);

        if(strlen(snon_output) != write(snon_fd, snon_output, strlen(snon_output)))
        {
            fprintf(stderr,"Error writing SNON sensor record\n");
            perror("write");
            return EXIT_FAILURE;
        }

        // Create sensor directory
        snprintf(snon_path, sizeof(snon_path), "%s/%s", output_path, sensor_uuid);
        if(-1 == mkdir(snon_path, S_IRWXU))
        {
            fprintf(stderr,"Error creating sensor directory\n");
            perror("mkdir");
            return EXIT_FAILURE;
        }

        cJSON_free(snon_output);
        cJSON_Delete(snon_root);
        close(snon_fd);
    }

    // ===========================================================
    // Obtain HXi Power Supply Information
    
    iso8601_time(working_string, sizeof(working_string));
    snprintf(snon_path, sizeof(snon_path), "%s/%s/%s-%s.json", output_path, sensor_uuid, sensor_uuid, working_string);

    if(-1 == hxi_collect_values(snon_path, dev_fd, sensor_uuid))
    {
        fprintf(stderr,"Error writing SNON value record\n");
        return EXIT_FAILURE;
    }

    close(dev_fd);
    return EXIT_SUCCESS;
}

void snon_addname(cJSON* snon_object, char* field, char* lang, char* name)
{
    cJSON*  name_object = NULL;

    name_object = cJSON_CreateObject();
    cJSON_AddItemToObjectCS(name_object, lang, cJSON_CreateString(name));
    cJSON_AddItemToObjectCS(snon_object, field, name_object);
}

// ===========================================================
// Read the contents of a JSON-formatted file into a cJSON
// object
// ===========================================================
cJSON* json_read_file(char* json_file_path)
{
    int             json_file = -1;
    struct stat     st;
    off_t           length = 0;
    char*           read_buffer = NULL;
    ssize_t         bytes_read = 0;
    cJSON*          json_contents = NULL;
    const char*     error_ptr = NULL;

    json_file = open(json_file_path, O_RDONLY);
    if(json_file == -1)
    {
        fprintf(stderr,"Unable to open JSON file '%s'.\n", json_file_path);
        return NULL;
    }

    // Get file size
    if(-1 == fstat(json_file, &st))
    {
        fprintf(stderr,"Unable to get size of JSON file '%s'.\n", json_file_path);
        close(json_file);
        return NULL;
    }
    
    length = st.st_size;

    // Allocate memory to read file
    read_buffer = (char*) malloc(length + 1);
    if(NULL == read_buffer)
    {
        fprintf(stderr,"Unable to allocate %ld bytes of memory to read JSON file.\n", length);
        close(json_file);
        return NULL;
    }

    // Read the file's contents
    bytes_read = read(json_file, read_buffer, length);
    close(json_file);

    if(bytes_read != length)
    {
        fprintf(stderr,"Unable to read file, expected %ld bytes, got %ld bytes.\n", length, bytes_read);
        free(read_buffer);
        return NULL;
    }

    read_buffer[length] = '\0';

    // Parse the contents of the file
    json_contents = cJSON_Parse(read_buffer);
    free(read_buffer);

    if(NULL == json_contents)
    {
        error_ptr = cJSON_GetErrorPtr();
        fprintf(stderr, "Error parsing JSON at: %s\n", error_ptr);
        return NULL;
    }

    return json_contents;
}

int hxi_collect_values(char* snon_path, int dev_fd, char* uuid_value)
{
    double  wattage = 0;
    int     snon_fd = -1;
    char*   snon_output;
    char    working_string[64];
    struct  sigaction sa;
    bool    first_record = true;
    cJSON*  senml_array = NULL;
    cJSON*  entry = NULL;
    size_t  data_len = 0;
    ssize_t data_written_len = 0;

    if (0 != access(snon_path, R_OK | W_OK))
    {
        // Set up a signal handler to end data collection
        sa.sa_handler = signal_handler;
        sa.sa_flags = SA_RESTART;

        sigemptyset(&sa.sa_mask);
        if (-1 == sigaction(SIGINT, &sa, NULL))
        {
            fprintf(stderr, "Error setting up signal handler\n");
            perror("sigaction");
            return -1;
        }

        // Create the sensor record file
        snon_fd = open(snon_path, O_RDWR | O_CREAT | O_TRUNC, S_IRUSR | S_IWUSR);
        if (-1 == snon_fd)
        {
            fprintf(stderr, "Error creating sensor record file\n");
            perror("open");
            return -1;
        }

        senml_array = cJSON_CreateArray();

        while (abort_collect == false)
        {
            iso8601_time(working_string, sizeof(working_string));

            if (-1 == hxi_read_power(dev_fd, 0, &wattage))
            {
                fprintf(stderr, "Error reading wattage value\n");
                cJSON_Delete(senml_array);
                close(snon_fd);
                return EXIT_FAILURE;
            }

            entry = cJSON_CreateObject();

            if (first_record)
            {
                snprintf(working_string, sizeof(working_string), "urn:uuid:%s", uuid_value);
                cJSON_AddItemToObjectCS(entry, "bn", cJSON_CreateString(working_string));
                first_record = false;
            }

            cJSON_AddItemToObjectCS(entry, "v", cJSON_CreateNumber(wattage));
            cJSON_AddItemToObjectCS(entry, "t", cJSON_CreateString(working_string));
            cJSON_AddItemToArray(senml_array, entry);

            usleep(100000);
        }

        // When abort signal received
        snon_output = cJSON_PrintUnformatted(senml_array);

        data_len = strlen(snon_output);
        data_written_len = write(snon_fd, snon_output, data_len);
        if ((size_t) data_written_len != data_len)
        {
            fprintf(stderr, "Error writing SNON value record\n");
            perror("SNON write");
            cJSON_free(snon_output);
            cJSON_Delete(senml_array);
            close(snon_fd);
            return EXIT_FAILURE;
        }

        cJSON_free(snon_output);
        cJSON_Delete(senml_array);
        close(snon_fd);
    }

    return 0;
}

int hxi_select_rail(int dev_fd, uint8_t rail)
{
    uint8_t     command[64];
    uint8_t     response[64];

    // Select the output rail to read
    memset(&command, 0, sizeof(command));
    memset(&response, 0, sizeof(response));
    command[0] = 0x02;
    command[1] = 0x00;  // Select Rail
    command[2] = rail;  // Desired Rail

    if(sizeof(command) != write(dev_fd, command, sizeof(command)))
    {
        fprintf(stderr,"Error writing command to device\n");
        perror("Command Write");
        return -1;
    }

    if(sizeof(response) != read(dev_fd, response, sizeof(response)))
    {
        fprintf(stderr,"Error reading response from device\n");
        perror("Command Read");
        return -1;
    }

    if(0x02 != response[0])
    {
        fprintf(stderr,"Unexpected response length %i (expected 2)\n", response[0]);
        return -1;
    }

     if(0x00 != response[1])
    {
        fprintf(stderr,"Unexpected response type '0x%02x' (expected 0x00)\n", response[1]);
        return -1;
    }

    return 0;
}

int hxi_read_power(int dev_fd, uint8_t rail, double* wattage)
{
    uint8_t     command[64];
    uint8_t     response[64];
    uint16_t    reg_contents = 0;

    if(-1 == hxi_select_rail(dev_fd, rail))
    {
        fprintf(stderr,"Unable to select output rail\n");
        return -1;
    }

    memset(&command, 0, sizeof(command));
    memset(&response, 0, sizeof(response));
    command[0] = 0x03;
    command[1] = 0x96;  // Read Power
    if(sizeof(command) != write(dev_fd, command, sizeof(command)))
    {
        fprintf(stderr,"Error writing command to device\n");
        perror("Command Write");
        return -1;
    }

    if(sizeof(response) != read(dev_fd, response, sizeof(response)))
    {
        fprintf(stderr,"Error reading response from device\n");
        perror("Command Read");
        return -1;
    }

    if(0x03 != response[0])
    {
        fprintf(stderr,"Unexpected response length %i (expected 3)\n", response[0]);
        return -1;
    }

     if(0x96 != response[1])
    {
        fprintf(stderr,"Unexpected response type '0x%02x' (expected 0x96)\n", response[1]);
        return -1;
    }

    reg_contents = response[2] + (response[3] << 8);
    *wattage = reg_to_value(reg_contents);

    return 0;
}

// Register to linear conversion
double reg_to_value(uint16_t reg)
{
    int16_t exponent;
    int32_t mantissa;
    double value;

    exponent = (int16_t) reg >> 11;
    mantissa = ((int16_t) ((reg & 0x7ff) << 5)) >> 5;

    if(exponent >= 0)
    {
        value = (double) mantissa * (1L << exponent);
    }
    else
    {
        value = (double) mantissa / (1L << -exponent);
    }

    return value;
}


bool is_valid_uuid(char* uuid_value)
{
    int i = 0;

    if(36 != strlen(uuid_value))
    {
        return false;
    }

    while(i < 36)
    {
        if(8 == i || 13 == i || 18 == i || 23 == i)
        {
            if('-' != uuid_value[i])
            {
                return false;
            }
        }
        else
        {
            if(0 == isxdigit((unsigned char) uuid_value[i]))
            {
                return false;
            }
        }

        i = i + 1;
    }

    return true;
}

int generate_random_uuid(char* uuid_value)
{
    uint8_t     uuid_bytes[16];
    int         urandom_fd = -1;

    urandom_fd = open("/dev/urandom", O_RDONLY);

    if(-1 == urandom_fd)
    {
        fprintf(stderr,"Unable to open /dev/urandom for UUID generation\n");
        perror("/dev/urandom");
        return -1;
    }

    if((ssize_t)sizeof(uuid_bytes) != read(urandom_fd, uuid_bytes, sizeof(uuid_bytes)))
    {
        fprintf(stderr,"Unable to read from /dev/urandom for UUID generation\n");
        perror("/dev/urandom");
        close(urandom_fd);
        return -1;
    }

    close(urandom_fd);

    uuid_bytes[6] = (uuid_bytes[6] & 0x0F) | 0x40;  // Version 4
    uuid_bytes[8] = (uuid_bytes[8] & 0x3F) | 0x80;  // Variant 1

    snprintf(uuid_value, 37,
        "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
        uuid_bytes[0],  uuid_bytes[1],  uuid_bytes[2],  uuid_bytes[3],
        uuid_bytes[4],  uuid_bytes[5],
        uuid_bytes[6],  uuid_bytes[7],
        uuid_bytes[8],  uuid_bytes[9],
        uuid_bytes[10], uuid_bytes[11], uuid_bytes[12],
        uuid_bytes[13], uuid_bytes[14], uuid_bytes[15]);

    return 0;
}


// Populates the buffer with the current ISO 8601 UTC date/time in microseconds
char* iso8601_time(char* buf, size_t len)
{
    struct timespec     ts;
    struct tm           t;

    if(buf == NULL || len < ISO8601_LEN)
    {
        return NULL;
    }

    if(0 != clock_gettime(CLOCK_REALTIME, &ts))
    {
        return NULL;
    }

    if(NULL == gmtime_r(&ts.tv_sec, &t))
    {
        return NULL;
    }

    snprintf(buf, len, "%04d-%02d-%02dT%02d:%02d:%02d.%06ldZ",
        t.tm_year + 1900,
        t.tm_mon  + 1,
        t.tm_mday,
        t.tm_hour,
        t.tm_min,
        t.tm_sec,
        (long)(ts.tv_nsec / 1000L));

    return buf;
}

// Signal Handler
static void signal_handler(int signum)
{
    struct sigaction    sa;

    if(SIGINT == signum)
    {
        // Set an abort flag for sigint
        abort_collect = true;
    }
    else
    {
        // Pass through other signals
        sa.sa_handler = SIG_DFL;
        sigemptyset(&sa.sa_mask);
        sa.sa_flags = 0;
        sigaction(signum, &sa, NULL);
        raise(signum);
    }
}

