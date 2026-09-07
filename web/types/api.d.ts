/**
 * OpenCR 的后端接口直接返回业务 JSON，不套 { code, data, message } 信封，
 * 错误通过 HTTP 状态码 + { error } 表达。各接口的具体类型见 src/common/apis/opencr.ts。
 */
export {}
