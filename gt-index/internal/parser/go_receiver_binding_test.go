package parser

import (
	"testing"
)

func TestGoTypedReceiverBindingsBecomeAssignments(t *testing.T) {
	res := parseFixture(t, "receiver.go", `package p

type Runner interface { Run() }

func Invoke(runner Runner) {
	var local Runner
	runner.Run()
	local.Run()
}
`)

	bindings := map[string]string{}
	for _, assignment := range res.Assignments {
		if assignment.File == "receiver.go" {
			bindings[assignment.VarName] = assignment.TypeName
		}
	}
	if bindings["runner"] != "Runner" {
		t.Fatalf("typed parameter binding=%q, want Runner (assignments=%+v)", bindings["runner"], res.Assignments)
	}
	if bindings["local"] != "Runner" {
		t.Fatalf("typed local binding=%q, want Runner (assignments=%+v)", bindings["local"], res.Assignments)
	}
}
