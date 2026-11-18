// The CommonJS build lives under dist/cjs; this file tells Node its .js files are CommonJS although the package is
// "type": "module".
import { writeFileSync } from "node:fs";

writeFileSync(new URL("../dist/cjs/package.json", import.meta.url), JSON.stringify({ type: "commonjs" }) + "\n");
