PY ?= python3

.PHONY: gen sample test trace eval-null eval-oracle eval-l0l1 eval-l0l1l2 eval-full l2-profile l3-profile demo multiseed adversarial clean

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

clean:
	rm -rf data/fixtures/run_* .pytest_cache
