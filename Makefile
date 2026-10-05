.PHONY: install test demo report serve
install:
	pip install -e ".[test]"
test:
	python -m pytest -q
demo:
	causeway demo --out runs --n 40
report:
	causeway report runs
serve:
	causeway serve runs --allow-program causeway.demo:SYSTEM --token dev-token
