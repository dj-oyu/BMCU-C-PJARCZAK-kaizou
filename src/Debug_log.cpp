#include "Debug_log.h"
#include "bmcu_link.h"
#include <string.h>
#include <stddef.h>

#include "ch32v20x_rcc.h"
#include "ch32v20x_gpio.h"
#include "ch32v20x_usart.h"
#include "ch32v20x_dma.h"
#include "ch32v20x_misc.h"

/* ===== IRQ ===== */
extern "C" void USART3_IRQHandler(void) __attribute__((interrupt("WCH-Interrupt-fast")));
extern "C" void USART3_IRQHandler(void)
{
    if (USART_GetITStatus(USART3, USART_IT_RXNE) != RESET)
    {
        bmcu_link_rx_isr_byte((uint8_t)USART_ReceiveData(USART3));
    }
}

void Debug_log_init(void) { }
uint64_t Debug_log_count64(void) { return 0ULL; }
void Debug_log_time(void) { }
void Debug_log_write(const void *data) { (void)data; }
void Debug_log_write_num(const void *data, int num) { (void)data; (void)num; }
__attribute__((used))
int _write(int fd, char *buf, int size)
{
#ifdef Debug_log_on
    (void)fd;
    Debug_log_write_num(buf, size);
#else
    (void)fd; (void)buf; (void)size;
#endif
    return size;
}

__attribute__((used))
void *_sbrk(ptrdiff_t incr)
{
    extern char _end[];
    extern char _heap_end[];
    static char *curbrk = _end;

    if ((curbrk + incr < _end) || (curbrk + incr > _heap_end))
        return (void *)(-1);

    curbrk += incr;
    return curbrk - incr;
}
