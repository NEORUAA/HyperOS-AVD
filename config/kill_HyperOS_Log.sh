#! /system/bin/sh

tags=(
    RecentsTaskLoader
    AurogonImmobulusMode
    ViewRootImplStubImpl
    RefreshRateSelector

    DynamicIslandEventCoordinator
    MiuiWallpaperSurfaceAnimation
    ActivityManagerWrapper

    MiuiDecorationDot
    MiuiDecorationBottom
    MiuiDecorationBase

    MIUIInput
    RenderEngine
    InsetsSource
    HwcComposer

    PassBlur
    TRUETONE
    NTKernel
)

function start_moon_log_kill ()
{
    for tag in "${tags[@]}"; do
        setprop log.tag.$tag S && sleep 1
        echo "Set [log.tag.$tag] Property"
    done
}
start_moon_log_kill
