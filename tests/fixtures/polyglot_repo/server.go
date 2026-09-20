// Go fixture: typed param callback + gin route.
package main

import "github.com/gin-gonic/gin"

func goHelper() string { return "ok" }

// Package-level callable alias: pkgHandler() inside a function resolves to
// goHelper via the callable-value rung.
var pkgHandler = goHelper

// Typed parameter: goWrap(goHelper) then cb() — argument→formal callable
// flow.
func goWrap(cb func() string) string { return cb() }

func goEntry() string { return goWrap(goHelper) }

func aliasEntry() string {
	f := goHelper
	return f()
}

func listUsers(c *gin.Context) {
	c.JSON(200, gin.H{"users": goEntry()})
	pkgHandler()
}

func main() {
	r := gin.Default()
	r.GET("/api/users", listUsers)
	_ = r.Run()
}
