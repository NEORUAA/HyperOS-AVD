// Run the platform ELF in mainline mode without entering the ARM32 APEX ELF.
// The loader still reads the original APEX BPF objects and delegates to the
// matching native platform bpfloader after loading the network programs.
.section .text
.global _start
.type _start, %function
_start:
    ldr x3, [sp]
    add x2, sp, #16
    add x2, x2, x3, lsl #3
    adr x0, executable
    adr x1, arguments
    mov x8, #221
    svc #0
    mov x0, #2
    adr x1, failure
    mov x2, #failure_end - failure
    mov x8, #64
    svc #0
    mov x0, #1
    mov x8, #93
    svc #0
.section .rodata
.balign 8
arguments:
    .quad identity, 0
executable:
    .asciz "/system/bin/netbpfload"
identity:
    .asciz "/apex/com.android.tethering/bin/netbpfload"
failure:
    .ascii "Watch5 native BPF loader exec failed\n"
failure_end:
