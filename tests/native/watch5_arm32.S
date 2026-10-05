.syntax unified
.arch armv7-a
.arm
.section .text
.global _start
.type _start, %function
_start:
    mov r0, #1
    adr r1, message
    mov r2, #18
    mov r7, #4
    svc #0
    mov r0, #0
    mov r7, #1
    svc #0
message:
    .ascii "watch5-aarch32-ok\n"
