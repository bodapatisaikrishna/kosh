PY ?= python3

.PHONY: gen sample test trace eval-null eval-oracle eval-l0l1 eval-l0l1l2 eval-full l2-profile l3-profile demo multiseed adversarial verify-deterministic freeze clean

gen:
	$(PY) -m data.generator.generate --records 2000 --seed 42 --months 3 --out data/fixtures/run_2000/

sample:
	$(PY) -m data.generator.generate --records 200 --seed 7 --months 1 --out data/fixtures/sample_200/

test:
	$(PY) -m pytest

trace:
	$(PY) -m data.generator.trace --fixtures data/fixtures/run_2000 --pick-clean

eval-null:
	$(PY) -m eval.report --fixtures data/fixtures/run_2000 --engine null --label null_run2000

eval-oracle:
	$(PY) -m eval.report --fixtures data/fixtures/run_2000 --engine oracle --label oracle_run2000

eval-l0l1:
	$(PY) -m eval.report --fixtures data/fixtures/run_2000 --engine l0l1 --label phase3

eval-l0l1l2:
	$(PY) -m eval.report --fixtures data/fixtures/run_2000 --engine l0l1l2 --label phase4

eval-full:
	$(PY) -m eval.report --fixtures data/fixtures/run_2000 --engine full --label phase5

l2-profile:
	$(PY) -m engine.l2_subset --profile --trials 2000 --seed 42

l3-profile:
	$(PY) -m engine.l3_agent --profile --backend nim --out benchmarks/phase5_synthetic.json

demo:
	$(PY) -m data.generator.generate --records 2000 --seed 42 --months 3 --out data/fixtures/run_2000/
	$(PY) -m eval.report --fixtures data/fixtures/run_2000 --engine full --label demo
	@echo ""
	@echo "Demo complete. Open benchmarks/run_demo.html for the full dashboard."

multiseed:
	$(PY) -m scripts.multiseed

adversarial:
	$(PY) -m scripts.run_adversarial

verify-deterministic:
	$(PY) -m pytest tests/test_determinism.py -v

# Task 7: regenerate the 3-scale frozen benchmark (500/2000/10000 records,
# seed=42, 3 months - the same params already on disk in data/fixtures/,
# per each dir's own generator-written manifest.json) plus phase3/4/5
# (l0l1/l0l1l2/full on run_2000) against final code. Previously a manual
# one-off run at each scale; this is the reusable target. eval.report always
# writes run_<label>.json/.html - freeze_*/phase* drop that prefix by
# established convention (README/RESULTS.md both link the short names), so
# each step is followed by the same rename the original one-off runs used.
freeze:
	$(PY) -m data.generator.generate --records 500 --seed 42 --months 3 --out data/fixtures/run_500/
	$(PY) -m eval.report --fixtures data/fixtures/run_500 --engine full --label freeze_500
	mv benchmarks/run_freeze_500.json benchmarks/freeze_500.json
	mv benchmarks/run_freeze_500.html benchmarks/freeze_500.html
	$(PY) -m data.generator.generate --records 2000 --seed 42 --months 3 --out data/fixtures/run_2000/
	$(PY) -m eval.report --fixtures data/fixtures/run_2000 --engine full --label freeze_2000
	mv benchmarks/run_freeze_2000.json benchmarks/freeze_2000.json
	mv benchmarks/run_freeze_2000.html benchmarks/freeze_2000.html
	$(PY) -m data.generator.generate --records 10000 --seed 42 --months 3 --out data/fixtures/run_10000/
	$(PY) -m eval.report --fixtures data/fixtures/run_10000 --engine full --label freeze_10000
	mv benchmarks/run_freeze_10000.json benchmarks/freeze_10000.json
	mv benchmarks/run_freeze_10000.html benchmarks/freeze_10000.html
	$(PY) -m eval.report --fixtures data/fixtures/run_2000 --engine l0l1 --label phase3
	mv benchmarks/run_phase3.json benchmarks/phase3.json
	mv benchmarks/run_phase3.html benchmarks/phase3.html
	$(PY) -m eval.report --fixtures data/fixtures/run_2000 --engine l0l1l2 --label phase4
	mv benchmarks/run_phase4.json benchmarks/phase4.json
	mv benchmarks/run_phase4.html benchmarks/phase4.html
	$(PY) -m eval.report --fixtures data/fixtures/run_2000 --engine full --label phase5
	mv benchmarks/run_phase5.json benchmarks/phase5.json
	mv benchmarks/run_phase5.html benchmarks/phase5.html

clean:
	rm -rf data/fixtures/run_* .pytest_cache
