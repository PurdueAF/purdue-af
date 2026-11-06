// promql-scope adds label matchers to every selector of a PromQL query.
//
// It reads {"query": "...", "matchers": [{"name", "type", "value"}]} from
// stdin and writes {"query": "..."} or {"error": "..."} to stdout. Parsing and
// printing with Prometheus' own parser keeps the query that runs identical to
// the one that was checked.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"

	"github.com/prometheus/prometheus/model/labels"
	"github.com/prometheus/prometheus/promql/parser"
)

type matcher struct {
	Name  string `json:"name"`
	Type  string `json:"type"`
	Value string `json:"value"`
}

type request struct {
	Query    string    `json:"query"`
	Matchers []matcher `json:"matchers"`
}

type response struct {
	Query string `json:"query,omitempty"`
	Error string `json:"error,omitempty"`
}

// Prometheus' defaults: no experimental functions, so info() and its implicit
// target_info lookup are refused.
var promql = parser.NewParser(parser.Options{})

var matchTypes = map[string]labels.MatchType{
	"=":  labels.MatchEqual,
	"!=": labels.MatchNotEqual,
	"=~": labels.MatchRegexp,
	"!~": labels.MatchNotRegexp,
}

func build(in []matcher) ([]*labels.Matcher, error) {
	out := make([]*labels.Matcher, 0, len(in))
	for _, m := range in {
		t, ok := matchTypes[m.Type]
		if !ok {
			return nil, fmt.Errorf("unknown matcher type %q", m.Type)
		}
		built, err := labels.NewMatcher(t, m.Name, m.Value)
		if err != nil {
			return nil, err
		}
		out = append(out, built)
	}
	return out, nil
}

// has reports whether a selector carries a matcher equal to want.
func has(selector *parser.VectorSelector, want *labels.Matcher) bool {
	for _, m := range selector.LabelMatchers {
		if m.Name == want.Name && m.Type == want.Type && m.Value == want.Value {
			return true
		}
	}
	return false
}

// Scope returns query with every selector also required to match enforced.
func Scope(query string, enforced []*labels.Matcher) (string, error) {
	expr, err := promql.ParseExpr(query)
	if err != nil {
		return "", err
	}
	parser.Inspect(expr, func(node parser.Node, _ []parser.Node) error {
		if selector, ok := node.(*parser.VectorSelector); ok {
			selector.LabelMatchers = append(selector.LabelMatchers, enforced...)
		}
		return nil
	})
	scoped := expr.String()
	// The printed query is what runs, so it is parsed again and checked.
	reparsed, err := promql.ParseExpr(scoped)
	if err != nil {
		return "", fmt.Errorf("the scoped query does not parse: %w", err)
	}
	var unscoped error
	parser.Inspect(reparsed, func(node parser.Node, _ []parser.Node) error {
		if selector, ok := node.(*parser.VectorSelector); ok {
			for _, m := range enforced {
				if !has(selector, m) {
					unscoped = errors.New("a selector lost its scope when printed")
				}
			}
		}
		return nil
	})
	if unscoped != nil {
		return "", unscoped
	}
	return scoped, nil
}

func run(in io.Reader) response {
	var req request
	if err := json.NewDecoder(in).Decode(&req); err != nil {
		return response{Error: "bad request: " + err.Error()}
	}
	enforced, err := build(req.Matchers)
	if err != nil {
		return response{Error: "bad matcher: " + err.Error()}
	}
	scoped, err := Scope(req.Query, enforced)
	if err != nil {
		return response{Error: err.Error()}
	}
	return response{Query: scoped}
}

func main() {
	if err := json.NewEncoder(os.Stdout).Encode(run(os.Stdin)); err != nil {
		os.Exit(1)
	}
}
