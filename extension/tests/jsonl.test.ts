import assert from "node:assert/strict";
import test from "node:test";

import { JSONLDecoder } from "../src/jsonl.js";

test("decodes arbitrarily chunked records", () => {
  const source = Buffer.from('{"text":"hello"}\n{"text":"world"}\n');
  const decoder = new JSONLDecoder<{ text: string }>();
  const records = [];

  for (const byte of source) {
    records.push(...decoder.push(Buffer.from([byte])));
  }
  records.push(...decoder.finish());

  assert.deepEqual(records, [{ text: "hello" }, { text: "world" }]);
});

test("preserves Unicode line separators and split UTF-8", () => {
  const decoder = new JSONLDecoder<{ text: string }>();
  const source = Buffer.from(`${JSON.stringify({ text: "before\u2028after 🐶" })}\n`);
  const records = [
    ...decoder.push(source.subarray(0, source.length - 2)),
    ...decoder.push(source.subarray(source.length - 2)),
    ...decoder.finish(),
  ];

  assert.deepEqual(records, [{ text: "before\u2028after 🐶" }]);
});

test("rejects incomplete and oversized records", () => {
  const incomplete = new JSONLDecoder();
  incomplete.push(Buffer.from("{}"));
  assert.throws(() => incomplete.finish(), /incomplete/);

  const oversized = new JSONLDecoder(2);
  assert.throws(() => oversized.push(Buffer.from("123")), /exceeds/);
});
