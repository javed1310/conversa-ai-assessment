import { responseSchemas } from "./response-schemas";

type Schema = { title?: string; $ref?: string; anyOf?: readonly Schema[]; oneOf?: readonly Schema[]; const?: unknown; enum?: readonly unknown[];
  type?: string; properties?: Record<string, Schema>; required?: readonly string[]; additionalProperties?: boolean | Schema;
  items?: Schema; minItems?: number; maxItems?: number; minLength?: number; maxLength?: number;
  pattern?: string; format?: string; minimum?: number; maximum?: number; exclusiveMinimum?: number; exclusiveMaximum?: number };
const schemas: Record<string, Schema> = responseSchemas;
export type ResponseSchema = keyof typeof responseSchemas | "ChatResultOrProcessing";

function timestamp(value: string): boolean {
  const parts = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!parts) return false;
  const [, year, month, day, hour, minute, second, zone] = parts;
  const days = new Date(Date.UTC(Number(year), Number(month), 0)).getUTCDate();
  return Number(month) >= 1 && Number(month) <= 12 && Number(day) >= 1 && Number(day) <= days &&
    Number(hour) <= 23 && Number(minute) <= 59 && Number(second) <= 59 &&
    (zone === "Z" || Number(zone.slice(1, 3)) <= 23 && Number(zone.slice(4)) <= 59) &&
    Number.isFinite(Date.parse(value));
}

function valid(value: unknown, schema: Schema, depth = 0): boolean {
  if (depth > 40) return false;
  if (schema.$ref) {
    const target = schemas[schema.$ref.split("/").at(-1)!];
    return !!target && valid(value, target, depth + 1);
  }
  if (schema.anyOf && !schema.anyOf.some(item => valid(value, item, depth + 1))) return false;
  if (schema.oneOf && schema.oneOf.filter(item => valid(value, item, depth + 1)).length !== 1) return false;
  if ("const" in schema && value !== schema.const) return false;
  if (schema.enum && !schema.enum.includes(value)) return false;
  switch (schema.type) {
    case "null": return value === null;
    case "boolean": return typeof value === "boolean";
    case "number": case "integer":
      return typeof value === "number" && Number.isFinite(value) &&
        (schema.type !== "integer" || Number.isInteger(value)) &&
        (schema.minimum === undefined || value >= schema.minimum) &&
        (schema.maximum === undefined || value <= schema.maximum) &&
        (schema.exclusiveMinimum === undefined || value > schema.exclusiveMinimum) &&
        (schema.exclusiveMaximum === undefined || value < schema.exclusiveMaximum);
    case "string": {
      if (typeof value !== "string") return false;
      const length = Array.from(value).length;
      return (schema.minLength === undefined || length >= schema.minLength) &&
        (schema.maxLength === undefined || length <= schema.maxLength) &&
        (!schema.pattern || new RegExp(schema.pattern).test(value)) &&
        (schema.format !== "date-time" || timestamp(value)) &&
        (schema.format !== "uuid" || /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value));
    }
    case "array": return Array.isArray(value) &&
      (schema.minItems === undefined || value.length >= schema.minItems) &&
      (schema.maxItems === undefined || value.length <= schema.maxItems) &&
      value.every(item => !schema.items || valid(item, schema.items, depth + 1));
    case "object": {
      if (!value || typeof value !== "object" || Array.isArray(value)) return false;
      const record = value as Record<string, unknown>;
      return (schema.required ?? []).every(key => Object.hasOwn(record, key)) &&
        Object.entries(record).every(([key, item]) => {
          const property = Object.hasOwn(schema.properties ?? {}, key) ? schema.properties![key] : undefined;
          return property ? valid(item, property, depth + 1) : schema.additionalProperties !== false &&
            (typeof schema.additionalProperties !== "object" || valid(item, schema.additionalProperties, depth + 1));
        });
    }
    default: return true;
  }
}

export function matchesSchema(value: unknown, name: ResponseSchema): boolean {
  return name === "ChatResultOrProcessing"
    ? valid(value, schemas.ChatResult) || valid(value, schemas.ProcessingResult)
    : valid(value, schemas[name]);
}
