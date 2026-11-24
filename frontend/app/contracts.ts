import Ajv2020 from "ajv/dist/2020";
import addFormats from "ajv-formats";
import type { components } from "../../packages/api-client/src/schema";
import rawSample from "../../contracts/sample.json";
import sampleSchema from "../../contracts/sample.schema.json";
import briefSchema from "../../contracts/brief.schema.json";

export type SampleBundle = components["schemas"]["SampleBundle"];
export type DraftBrief = components["schemas"]["DraftBrief"];
export type DecisionReport = components["schemas"]["DecisionReport"];
export type EvidencePassage = components["schemas"]["EvidencePassage"];
export type Calculation = components["schemas"]["Calculation"];
export type Scenario = SampleBundle["scenarios"][number];

const ajv = new Ajv2020({ strict: false, allErrors: true });
addFormats(ajv);
const validateSample = ajv.compile<SampleBundle>(sampleSchema);
export const validateDraft = ajv.compile<DraftBrief>(briefSchema);
if (!validateSample(rawSample)) {
  throw new Error(
    "Generated fictional sample does not match the Python contract. Run pnpm contracts.",
  );
}
export const sample: SampleBundle = rawSample;
