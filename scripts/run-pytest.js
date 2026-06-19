const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const repoRoot = path.resolve(__dirname, '..');
const venvPython = process.platform === 'win32'
  ? path.join(repoRoot, '.venv', 'Scripts', 'python.exe')
  : path.join(repoRoot, '.venv', 'bin', 'python');
const suites = [
  { cwd: path.join(repoRoot, 'query_api'), args: ['-m', 'pytest', 'tests', '-v'] },
  { cwd: path.join(repoRoot, 'indexing_service'), args: ['-m', 'pytest', 'tests', '-v'] },
];

const pythonCandidates = process.env.PYTHON
  ? [process.env.PYTHON]
  : fs.existsSync(venvPython)
    ? [venvPython, ...(process.platform === 'win32' ? ['python', 'py'] : ['python3', 'python'])]
  : process.platform === 'win32'
    ? ['python', 'py']
    : ['python3', 'python'];

function runSuite(command, suite) {
  return spawnSync(command, suite.args, {
    cwd: suite.cwd,
    stdio: 'inherit',
    env: { ...process.env, TESTING: process.env.TESTING || '1' },
  });
}

for (const command of pythonCandidates) {
  let missing = false;
  for (const suite of suites) {
    const result = runSuite(command, suite);
    if (result.error && result.error.code === 'ENOENT') {
      missing = true;
      break;
    }
    if (result.status !== 0) {
      process.exit(result.status ?? 1);
    }
  }
  if (!missing) {
    process.exit(0);
  }
}

console.error(`Unable to find a Python executable. Tried: ${pythonCandidates.join(', ')}`);
process.exit(1);
