package main

import "path/filepath"

func Scan(root string) {
	filepath.Walk(root, nil)
}
