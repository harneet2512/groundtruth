//go:build !windows

package main

import "os"

func replacePublishedFile(staged, target string) error {
	return os.Rename(staged, target)
}
