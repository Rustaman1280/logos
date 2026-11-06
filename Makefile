PYTHON ?= python3

.PHONY: test fmt clean

test:
	$(PYTHON) -m unittest discover -s tests -t . -v

fmt:
	$(PYTHON) -m logos fmt .

clean:
	rm -rf build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -f *.o *.ll
