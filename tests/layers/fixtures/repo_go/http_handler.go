package server

import (
	"fmt"
	"net/http"
)

// httpHandler is the top-level HTTP request handler for the example service.
// It validates the incoming request and dispatches to the parser.
func httpHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "only GET supported", http.StatusMethodNotAllowed)
		return
	}
	parsed, err := ParseRequestURL(r.URL.String())
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	fmt.Fprintf(w, "host=%s path=%s", parsed.Host, parsed.Path)
}

// RegisterRoutes wires the handler onto the default mux.
func RegisterRoutes() {
	http.HandleFunc("/", httpHandler)
}
