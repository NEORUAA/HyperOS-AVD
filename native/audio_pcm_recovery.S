// Pinned libtinyalsav2.so: recover one playback EIO without changing its ABI.
// Link .text at 0x18000, original_transfer=0xc8bc, original_errno=0xe760,
// original_stop=0xb848 and original_prepare=0xbda0. No dynamic relocations.
.text
.global recover_pcm_writei
.type recover_pcm_writei, %function
recover_pcm_writei:
    stp x29, x30, [sp, #-64]!
    mov x29, sp
    stp x19, x20, [sp, #16]
    stp x21, x22, [sp, #32]
    mov x19, x0
    mov x20, x1
    mov w21, w2
    bl original_transfer
    mov w22, w0
    tbz w0, #31, done
    cmn w22, #1 // Only recover the ioctl failure, not argument errors.
    b.ne unchanged
    bl original_errno
    ldr w8, [x0]
    cmp w8, #5 // EIO
    b.ne unchanged
    ldr w8, [x19, #4]
    tbnz w8, #30, unchanged // PCM_NORESTART
    mov x0, x19
    bl original_stop
    cbnz w0, failed
    mov x0, x19
    bl original_prepare
    cbnz w0, failed
    mov x0, x19
    mov x1, x20
    mov w2, w21
    bl original_transfer // One retry; propagate the real result.
    b done
failed:
    bl original_errno
    mov w8, #5
    str w8, [x0] // Preserve the original EIO if recovery fails.
unchanged:
    mov w0, w22
done:
    ldp x21, x22, [sp, #32]
    ldp x19, x20, [sp, #16]
    ldp x29, x30, [sp], #64
    ret
.size recover_pcm_writei, .-recover_pcm_writei
