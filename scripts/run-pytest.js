const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const repoRoot = path.resolve(__dirname, '..');
const minimumPython = [3, 11];
const venvPython = process.platform === 'win32'
  ? path.join(repoRoot, '.venv', 'Scripts', 'python.exe')
  : path.join(repoRoot, '.venv', 'bin', 'python');
const suites = [
  { cwd: path.join(repoRoot, 'query_api'), args: ['-m', 'pytest', 'tests', '-v'] },
  { cwd: path.join(repoRoot, 'indexing_service'), args: ['-m', 'pytest', 'tests', '-v'] },
];

function resolvePythonCommand(command) {
  if (
    path.isAbsolute(command)
    || (!command.includes('/') && !command.includes('\\'))
  ) {
    return command;
  }
  return path.resolve(repoRoot, command);
}

const pythonCandidates = process.env.PYTHON
  ? [resolvePythonCommand(process.env.PYTHON)]
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

function checkPythonVersion(command) {
  const result = spawnSync(command, [
    '-c',
    `import sys; raise SystemExit(0 if sys.version_info >= (${minimumPython[0]}, ${minimumPython[1]}) else 42)`,
  ], {
    cwd: repoRoot,
    stdio: 'ignore',
  });
  if (result.error && result.error.code === 'ENOENT') {
    return 'missing';
  }
  return result.status === 0 ? 'supported' : 'unsupported';
}

const unsupportedPython = [];
for (const command of pythonCandidates) {
  const versionStatus = checkPythonVersion(command);
  if (versionStatus === 'missing') {
    continue;
  }
  if (versionStatus === 'unsupported') {
    unsupportedPython.push(command);
    continue;
  }

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

console.error(
  `Unable to find a Python ${minimumPython.join('.')}+ executable. Tried: ${pythonCandidates.join(', ')}`
);
if (unsupportedPython.length > 0) {
  console.error(`Skipped unsupported Python executables: ${unsupportedPython.join(', ')}`);
}
process.exit(1);
