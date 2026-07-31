# Multi-BMCU runtime

`BMCU_LINKS` configures up to two independently indexed UART links. UART input
is drained round-robin under a total byte budget. Every validated frame keeps
its physical `link_index` in the BMB1 header.

The BMB1 transport sequence is global per Pico boot. ACK is a global
contiguous watermark; it is not link-scoped. STATUS is coalesced before a
sequence is allocated, while EVENT and link/loss records are protected.

The local UI uses the binary endpoints documented in
`docs/PICO_BAMBUDDY_OUTPUT.md`.
