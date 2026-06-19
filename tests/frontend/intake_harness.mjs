// Load docs/intake/survey.js into a sandbox (no DOM) and run its pure core over
// an answers JSON passed as argv[2]. Prints {errors, intake} so the Python test
// can assert validation and feed the intake straight into tooling/scaffold.py.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..', '..');
const SURVEY_JS = join(ROOT, 'docs', 'intake', 'survey.js');

const sandbox = {};
sandbox.self = sandbox;          // survey.js's IIFE binds its global to `self`
sandbox.globalThis = sandbox;
const context = vm.createContext(sandbox);
vm.runInContext(readFileSync(SURVEY_JS, 'utf8'), context);

const SurveyIntake = sandbox.SurveyIntake;
const answers = JSON.parse(readFileSync(process.argv[2], 'utf8'));

process.stdout.write(JSON.stringify({
  errors: SurveyIntake.validateAnswers(answers),
  intake: SurveyIntake.surveyToIntake(answers),
}));
