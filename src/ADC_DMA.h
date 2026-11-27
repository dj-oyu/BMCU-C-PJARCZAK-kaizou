#pragma once

#include <stdbool.h>
#include <stdint.h>

void  ADC_DMA_init(void);
bool  ADC_DMA_is_inited(void);

void  ADC_DMA_gpio_analog(void);

void  ADC_DMA_poll(void);
// Boxcar average of the last four DMA blocks per ADC channel, in 0.1 mV
// (sensor_fixed.h). 3.3 V reads 33000.
const int32_t* ADC_DMA_get_value(void);

void  ADC_DMA_filter_reset(void);
bool  ADC_DMA_ready(void);
void  ADC_DMA_wait_full(void);
