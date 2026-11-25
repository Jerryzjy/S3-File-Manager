.PHONY: install-local test

install-local:
	python -m pip install -e .

test:
	python -m unittest discover -s tests -v
