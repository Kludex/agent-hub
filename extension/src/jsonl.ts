import { StringDecoder } from "node:string_decoder";

export class JSONLDecoder<T> {
  private readonly decoder = new StringDecoder("utf8");
  private buffer = "";

  constructor(private readonly maxRecordBytes = 1024 * 1024) {}

  push(chunk: Buffer): T[] {
    this.buffer += this.decoder.write(chunk);
    return this.drain();
  }

  finish(): T[] {
    this.buffer += this.decoder.end();
    const records = this.drain();
    if (this.buffer.length > 0) {
      throw new Error("JSONL stream ended with an incomplete record");
    }
    return records;
  }

  private drain(): T[] {
    const records: T[] = [];
    while (true) {
      const newline = this.buffer.indexOf("\n");
      if (newline < 0) {
        if (Buffer.byteLength(this.buffer) > this.maxRecordBytes) {
          throw new Error("JSONL record exceeds the configured limit");
        }
        return records;
      }
      let line = this.buffer.slice(0, newline);
      this.buffer = this.buffer.slice(newline + 1);
      if (line.endsWith("\r")) {
        line = line.slice(0, -1);
      }
      if (Buffer.byteLength(line) > this.maxRecordBytes) {
        throw new Error("JSONL record exceeds the configured limit");
      }
      if (line.length > 0) {
        records.push(JSON.parse(line) as T);
      }
    }
  }
}
