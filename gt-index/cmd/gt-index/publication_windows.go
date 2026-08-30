//go:build windows

package main

import (
	"fmt"
	"syscall"
	"unsafe"
)

const (
	moveFileReplaceExisting = 0x1
	moveFileWriteThrough    = 0x8
)

var moveFileExW = syscall.NewLazyDLL("kernel32.dll").NewProc("MoveFileExW")

func replacePublishedFile(staged, target string) error {
	stagedUTF16, err := syscall.UTF16PtrFromString(staged)
	if err != nil {
		return err
	}
	targetUTF16, err := syscall.UTF16PtrFromString(target)
	if err != nil {
		return err
	}
	ok, _, callErr := moveFileExW.Call(
		uintptr(unsafe.Pointer(stagedUTF16)),
		uintptr(unsafe.Pointer(targetUTF16)),
		uintptr(moveFileReplaceExisting|moveFileWriteThrough),
	)
	if ok == 0 {
		return fmt.Errorf("MoveFileExW: %w", callErr)
	}
	return nil
}
