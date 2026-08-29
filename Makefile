PY ?= python3

.PHONY: gen sample test trace clean

gen:
	$(PY) -m data.generator.generate --records 2000 --seed 42 --months 3 --out data/fixtures/run_2000/

sample:
	$(PY) -m data.generator.generate --records 200 --seed 7 --months 1 --out data/fixtures/sample_200/

test:
	$(PY) -m pytest

trace:
	$(PY) -m data.generator.trace --fixtures data/fixtures/run_2000 --pick-clean

clean:
	rm -rf data/fixtures/run_* .pytest_cache
