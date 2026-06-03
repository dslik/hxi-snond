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
#include <string.h>
#include <unistd.h>
#include <getopt.h>
#include <fcntl.h>
#include <sys/ioctl.h>

// Linux includes
#include <linux/hidraw.h>

// Prototypes
int hxi_read_power(int dev_fd, uint8_t rail, double* wattage);
int hxi_select_rail(int dev_fd, uint8_t rail);
double reg_to_value(uint16_t reg);
bool is_valid_uuid(char* uuid_value);
int generate_random_uuid(char* uuid_value);


// Global Variables
const char* usage = "Usage: hxi-snond [options]\n"
                    "\n"
                    "Options:\n"
                    "  -d DEVICE   Hidraw device path (default: /dev/hidraw0)\n"
                    "  -o OUTPUT   Location to write SNON files (default: current working directory)\n"
                    "  -f FREQUENCY   Measurement read frequency in seconds (default: 0.25)\n"
                    "  -u UUID        Sensor UUID (default: randomly generated)\n"
                    "  -h          Show this help message\n";


int main(int argc, char *argv[])
{
    int                     opt = 0;
    const char*             device_name = "/dev/hidraw0";
    const char*             output_path = "./";
    const char*             frequency_arg = "0.25";
    const char*             uuid_arg = NULL;
    char                    uuid_value[37];
    int                     dev_fd = 0;
    struct hidraw_devinfo   hid_info;
    double                  wattage = 0;
    double                  frequency = 0;

    // Obtain command line arguments
    while(-1 != opt)
    {
        opt = getopt(argc, argv, "d:o:h");

        switch(opt)
        {
            case 'd':
                device_name = optarg;
                break;
            case 'o':
                output_path = optarg;
                break;
            case 'f':
                frequency_arg = optarg;
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

    // Ensure that the output directory ends with a "/"
    if('/' != output_path[strlen(output_path) - 1])
    {
        fprintf(stderr,"Specified output directory does not end with a '/': %s\n", output_path);
        return EXIT_FAILURE;
    }

    // Validate frequency
    frequency = strtod(frequency_arg, NULL);

    if(0 >= frequency)
    {
        fprintf(stderr,"Measurement frequency must be greater than zero: %s\n", frequency_arg);
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

        memcpy(uuid_value, uuid_arg, sizeof(uuid_value));
    }
    else
    {
        if(-1 == generate_random_uuid(uuid_value))
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
    // Obtain HXi Power Supply Information
    if(-1 == hxi_read_power(dev_fd, 0, &wattage))
    {
        fprintf(stderr,"Error reading wattage value\n");
        return EXIT_FAILURE;
    }

    // Test printing out wattage, replace with SNON code
    printf("Wattage %g\n", wattage);

    close(dev_fd);

    return EXIT_SUCCESS;
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

    if(0 >= urandom_fd)
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