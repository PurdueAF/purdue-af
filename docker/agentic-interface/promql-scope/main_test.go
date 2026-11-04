package main

import (
	"strings"
	"testing"

	"github.com/prometheus/prometheus/model/labels"
	"github.com/prometheus/prometheus/promql/parser"
)

var enforced = []*labels.Matcher{
	labels.MustNewMatcher(labels.MatchRegexp, "namespace", "|cms"),
	labels.MustNewMatcher(labels.MatchRegexp, "username", "|alice"),
}

func selectors(t *testing.T, query string) []*parser.VectorSelector {
	t.Helper()
	expr, err := promql.ParseExpr(query)
	if err != nil {
		t.Fatalf("%q does not parse: %v", query, err)
	}
	var found []*parser.VectorSelector
	parser.Inspect(expr, func(node parser.Node, _ []parser.Node) error {
		if s, ok := node.(*parser.VectorSelector); ok {
			found = append(found, s)
		}
		return nil
	})
	return found
}

func TestEverySelectorIsScoped(t *testing.T) {
	queries := []string{
		`up`,
		`sum(x) - sum(x{username!="alice"})`,
		`rate(container_cpu_usage_seconds_total{pod=~"purdue-af-.*"}[5m] offset 1h)`,
		`max_over_time(rate(y{job="j"}[1m])[10m:1m])`,
		`x @ start() and on(pod) y`,
		`label_replace(up, "a", "$1", "b", "(.*)")`,
		`{__name__=~".+"}`,
		`{"utf8.metric.name"}`,
		`count by (username) (af_home_dir_util)`,
		`histogram_quantile(0.9, sum by (le) (rate(h_bucket[5m])))`,
		"x{a=`\\d+\"}`}",
		`x{a="q\"} or y{b=\"z"}`,
		`-x unless y`,
		`absent_over_time(z[1h])`,
	}
	for _, q := range queries {
		scoped, err := Scope(q, enforced)
		if err != nil {
			t.Fatalf("%q: %v", q, err)
		}
		found := selectors(t, scoped)
		if len(found) != len(selectors(t, q)) {
			t.Fatalf("%q -> %q changed the number of selectors", q, scoped)
		}
		for _, s := range found {
			for _, m := range enforced {
				if !has(s, m) {
					t.Fatalf("%q -> %q: selector %s lacks %s", q, scoped, s, m)
				}
			}
		}
	}
}

func TestUserMatchersStay(t *testing.T) {
	scoped, err := Scope(`x{username="bob"}`, enforced)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(scoped, `username="bob"`) || !strings.Contains(scoped, `username=~"|alice"`) {
		t.Fatalf("both matchers must apply: %q", scoped)
	}
}

func TestRefusals(t *testing.T) {
	for _, q := range []string{``, `sum(`, `info(up)`, `x{a="1" or b="2"}`} {
		if _, err := Scope(q, enforced); err == nil {
			t.Fatalf("%q was accepted", q)
		}
	}
}

func TestRun(t *testing.T) {
	got := run(strings.NewReader(`{"query": "up", "matchers": [{"name": "namespace", "type": "=~", "value": "|cms"}]}`))
	if got.Error != "" || got.Query != `up{namespace=~"|cms"}` {
		t.Fatalf("got %+v", got)
	}
	if got := run(strings.NewReader(`{"query": "up", "matchers": [{"name": "a", "type": "~", "value": "x"}]}`)); got.Error == "" {
		t.Fatal("an unknown matcher type was accepted")
	}
	if got := run(strings.NewReader(`{"query": "up", "matchers": [{"name": "a", "type": "=~", "value": "("}]}`)); got.Error == "" {
		t.Fatal("a bad regex was accepted")
	}
}
