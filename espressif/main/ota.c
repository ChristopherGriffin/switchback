// This file is part of switchbota (https://github.com/kendallgoto/switchbota/).
// Copyright (c) 2022 Kendall Goto.

// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, version 3.

// This program is distributed in the hope that it will be useful, but
// WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
// General Public License for more details.

// You should have received a copy of the GNU General Public License
// along with this program. If not, see <http://www.gnu.org/licenses/>.

#include <stdio.h>
#include <stdlib.h>
#include <sys/param.h>

#include "esp_system.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_http_client.h"
#include "esp_partition.h"
#include "esp_ota_ops.h"
#include "esp_spi_flash.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "rom/md5_hash.h"

#include "config.h"

#include "ota.h"

static const char *TAG = "ota";

static void ota_task(void *pvParameters);
void ota_init()
{
    ESP_LOGI(TAG, "ota_init");
    
    xTaskCreate(&ota_task, "otaTask", 32768, NULL, 5, NULL);
}

esp_err_t ota_event_handler(esp_http_client_event_t *evt)
{
    switch(evt->event_id) {
        case HTTP_EVENT_ERROR:
            ESP_LOGE(TAG, "HTTP_EVENT_ERROR");
            break;
        case HTTP_EVENT_ON_CONNECTED:
            ESP_LOGI(TAG, "HTTP_EVENT_ON_CONNECTED");
            break;
        case HTTP_EVENT_HEADER_SENT:
            ESP_LOGD(TAG, "HTTP_EVENT_HEADER_SENT");
            break;
        case HTTP_EVENT_ON_HEADER:
            ESP_LOGD(TAG, "HTTP_EVENT_ON_HEADER");
            break;
        case HTTP_EVENT_ON_DATA:
            ESP_LOGD(TAG, "HTTP_EVENT_ON_DATA, len=%d", evt->data_len);
            break;
        case HTTP_EVENT_ON_FINISH:
            ESP_LOGI(TAG, "HTTP_EVENT_ON_FINISH");
            break;
        case HTTP_EVENT_DISCONNECTED:
            ESP_LOGI(TAG, "HTTP_EVENT_DISCONNECTED");
            break;
    }
    return ESP_OK;
}
void dump(const void* data, size_t size)
{
	char ascii[17];
	size_t i, j;
	ascii[16] = '\0';
	for (i = 0; i < size; ++i) {
		printf("%02X ", ((unsigned char*)data)[i]);
		if (((unsigned char*)data)[i] >= ' ' && ((unsigned char*)data)[i] <= '~') {
			ascii[i % 16] = ((unsigned char*)data)[i];
		} else {
			ascii[i % 16] = '.';
		}
		if ((i+1) % 8 == 0 || i+1 == size) {
			printf(" ");
			if ((i+1) % 16 == 0) {
				printf("|  %s \n", ascii);
			} else if (i+1 == size) {
				ascii[(i+1) % 16] = '\0';
				if ((i+1) % 16 <= 8) {
					printf(" ");
				}
				for (j = (i+1) % 16; j < 16; ++j) {
					printf("   ");
				}
				printf("|  %s \n", ascii);
			}
		}
	}
}
int verify_checksum(unsigned char *result, unsigned char *comparison)
{
    int equal = 1;
    for(int i = 0; i < 16; i++) {
        if(result[i] != comparison[i]) {
            equal = 0;
        }
    }
    return equal;
}

unsigned char buf[OTA_BUF];

// send our Wi-Fi MAC with each request so the server can tell plugs apart
void ota_set_mac_header(esp_http_client_handle_t client)
{
    char macStr[18];
    uint8_t mac[6] = { 0 };
    if (esp_wifi_get_mac(WIFI_IF_STA, mac) != ESP_OK) {
        return;
    }
    snprintf(macStr, sizeof(macStr), "%02x:%02x:%02x:%02x:%02x:%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    esp_http_client_set_header(client, "X-Device-MAC", macStr);
}

int hex_value(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

// GET payload.md5 and parse "<md5hex> <size>". Returns 1 only if both fields are valid.
int ota_fetch_manifest(unsigned char *md5, int *size)
{
    char body[64] = { 0 };
    int len = 0;
    int status = 0;

    esp_http_client_config_t config = {
        .url = BINARY_MD5_URL,
        .event_handler = ota_event_handler,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) {
        return 0;
    }
    ota_set_mac_header(client);
    if (esp_http_client_open(client, 0) == ESP_OK) {
        esp_http_client_fetch_headers(client);
        status = esp_http_client_get_status_code(client);
        int count;
        while (len < (int)sizeof(body) - 1 &&
               (count = esp_http_client_read(client, body + len, sizeof(body) - 1 - len)) > 0) {
            len += count;
        }
    }
    esp_http_client_close(client);
    esp_http_client_cleanup(client);

    if (status != 200 || len < 34 || body[32] != ' ') {
        ESP_LOGE(TAG, "bad payload.md5 response (status %d, %d bytes)", status, len);
        return 0;
    }
    for (int i = 0; i < 16; i++) {
        int hi = hex_value(body[i * 2]);
        int lo = hex_value(body[i * 2 + 1]);
        if (hi < 0 || lo < 0) {
            ESP_LOGE(TAG, "bad payload.md5 hash");
            return 0;
        }
        md5[i] = (hi << 4) | lo;
    }
    char *end = NULL;
    long parsed = strtol(body + 33, &end, 10);
    if (end == body + 33 || parsed <= 0) {
        ESP_LOGE(TAG, "bad payload.md5 size");
        return 0;
    }
    *size = (int)parsed;
    return 1;
}

void ota_flip() {
    esp_http_client_config_t config = {
        .url = FALLBACK_URL,
        .event_handler = ota_event_handler,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    ota_set_mac_header(client);
    esp_http_client_open(client, 0);
    esp_http_client_fetch_headers(client);
    const esp_partition_t *update_partition = esp_ota_get_next_update_partition(NULL);
    esp_ota_handle_t update_handle = 0;
    int bytesWritten = 0;
    bool first_byte = false;
    while (1) {
        int data_read = esp_http_client_read(client, (char *)buf, OTA_BUF);
        if (data_read > 0) {
            if (first_byte == false) {
                    first_byte = true;
                    esp_ota_begin(update_partition, OTA_WITH_SEQUENTIAL_WRITES, &update_handle);
            }
            esp_ota_write( update_handle, (const void *)buf, data_read);
            bytesWritten += data_read;
            ESP_LOGD(TAG, "Written image length %d", bytesWritten);
        } else {
            break;
        }
    }
    ESP_LOGI(TAG, "Total Write binary data length: %d", bytesWritten);
    esp_ota_end(update_handle);

    esp_ota_set_boot_partition(update_partition);
    ESP_LOGI(TAG, "Prepare to restart system!");
    esp_restart();
}

void ota_inject() {
    unsigned char expectedMD5[16] = { 0 };
    int expectedSize = 0;

    // Everything before the erase is a preflight. If any of it fails, flash is left alone.
    if (!ota_fetch_manifest(expectedMD5, &expectedSize)) {
        ESP_LOGE(TAG, "no usable payload.md5, refusing to erase");
        vTaskDelay(5000 / portTICK_PERIOD_MS);
        return;
    }
    if (expectedSize > WRITE_SIZE) {
        ESP_LOGE(TAG, "payload is %d bytes, limit is %d, refusing to erase", expectedSize, WRITE_SIZE);
        vTaskDelay(5000 / portTICK_PERIOD_MS);
        return;
    }

    esp_http_client_config_t config = {
        .url = BINARY_URL,
        .event_handler = ota_event_handler,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    int count = 0;
    int preflightOk = 0;
    if (client != NULL) {
        ota_set_mac_header(client);
        if (esp_http_client_open(client, 0) == ESP_OK) {
            int totalBytes = esp_http_client_fetch_headers(client);
            int status = esp_http_client_get_status_code(client);
            if (status == 200 && totalBytes == expectedSize) {
                // hold the first chunk until we know it starts with an ESP32-C3 image header
                // (magic 0xE9, chip id 0x0005), which is what a factory image begins with
                count = esp_http_client_read(client, (char *)buf, OTA_BUF);
                preflightOk = count >= 16 && buf[0] == 0xE9 && buf[12] == 0x05 && buf[13] == 0x00;
            } else {
                ESP_LOGE(TAG, "payload.bin status %d, length %d, expected %d", status, totalBytes, expectedSize);
            }
        }
    }
    if (!preflightOk) {
        ESP_LOGE(TAG, "payload.bin preflight failed, refusing to erase");
        if (client != NULL) {
            esp_http_client_close(client);
            esp_http_client_cleanup(client);
        }
        vTaskDelay(5000 / portTICK_PERIOD_MS);
        return;
    }

    // From here on a reboot means a dead plug, so nothing below may abort. Failures fall
    // through to the caller's loop and the whole pass is retried.
    int eraseSize = (expectedSize + 0xFFF) & ~0xFFF;
    ESP_LOGI(TAG, "erasing bootloader! don't lose power now!");
    int flashOk = esp_flash_erase_region(NULL, 0, eraseSize) == ESP_OK;
    int readBytes = 0;

    struct MD5Context hashCalculate = { 0 };
    unsigned char hashData[16] = { 0 };
    MD5Init(&hashCalculate);
    while(flashOk && count > 0 && readBytes + count <= expectedSize) {
        MD5Update(&hashCalculate, buf, count); // calculate MD5 as we download
        flashOk = esp_flash_write(NULL, buf, readBytes, count) == ESP_OK;
        readBytes += count;
        ESP_LOGI(TAG, "flashed %d / %d bytes", readBytes, expectedSize);
        vTaskDelay(20 / portTICK_PERIOD_MS);
        count = esp_http_client_read(client, (char *)buf, OTA_BUF);
    }
    MD5Final(hashData, &hashCalculate);
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    if(flashOk && readBytes == expectedSize && verify_checksum(hashData, expectedMD5)) {
        ESP_LOGI(TAG, "Download MD5 OK, verifying SPI");
        // verify written data from SPI
        struct MD5Context spiMD5 = { 0 };
        unsigned char spiHashData[16] = { 0 };
        MD5Init(&spiMD5);
        for(int i = 0; i < expectedSize; i += MIN(OTA_BUF, (expectedSize-i))) {
            int bytesToRead = MIN(OTA_BUF, (expectedSize-i));
            esp_flash_read(NULL, buf, i, bytesToRead);
            MD5Update(&spiMD5, buf, bytesToRead); // calculate MD5 as we download
            //dump(buf, bytesToRead);
            ESP_LOGI(TAG, "read %d / %d bytes", i, expectedSize);
            vTaskDelay(20 / portTICK_PERIOD_MS);
        }
        MD5Final(spiHashData, &spiMD5);
        if(verify_checksum(spiHashData, expectedMD5)) {
            ESP_LOGI(TAG, "SPI Write OK - restarting ...");
            esp_restart();
        } else {
            ESP_LOGE(TAG, "SPI Write BAD, retrying ...");
        }
        vTaskDelay(5000 / portTICK_PERIOD_MS);
    } else {
        ESP_LOGE(TAG, "Download BAD (%d / %d bytes), retrying ...", readBytes, expectedSize);
        vTaskDelay(5000 / portTICK_PERIOD_MS);
    }
}
void ota_task(void *pvParameters)
{
    // check if we're in an ok OTA position ...
    ESP_LOGI(TAG, "ota_task ready");
    const esp_partition_t *currentPartition = esp_ota_get_running_partition();
    if(currentPartition->address < WRITE_SIZE) {
        ESP_LOGI(TAG, "OTA_0 detected!");
        ota_flip();
        return;
    } else {
        while(1) {
            ota_inject();
        }
    }
}

