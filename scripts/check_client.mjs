import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const directory = mkdtempSync(join(tmpdir(), "round-robin-contract-"));
try {
  for (const [source, target] of [['contracts/openapi.json', 'packages/api-client/src/schema.ts'], ['contracts/application.openapi.json', 'packages/api-client/src/application.ts']]) {
  const output = join(directory, "schema.ts");
  execFileSync(
    "pnpm",
    ["exec", "openapi-typescript", source, "-o", output],
    { stdio: "pipe" },
  );
  if (
    !readFileSync(output).equals(
      readFileSync(target),
    )
  ) {
    throw new Error(
      "Generated TypeScript differs from the Python OpenAPI contract. Run pnpm contracts.",
    );
  }
  }
  process.stdout.write("Generated TypeScript contract matches.\n");
} finally {
  rmSync(directory, { recursive: true, force: true });
}
