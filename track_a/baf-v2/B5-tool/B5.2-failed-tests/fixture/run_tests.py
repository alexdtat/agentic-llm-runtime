import sys
import unittest
import test_project

suite = unittest.defaultTestLoader.loadTestsFromModule(test_project)
result = unittest.TestResult()
suite.run(result)
for test, _ in result.failures + result.errors:
    print('FAIL: ' + test.id(), file=sys.stderr)
print(f'Ran {result.testsRun} tests', file=sys.stderr)
if result.wasSuccessful():
    print('OK', file=sys.stderr)
else:
    print(f'FAILED (failures={len(result.failures)}, errors={len(result.errors)})', file=sys.stderr)
sys.exit(0 if result.wasSuccessful() else 1)
