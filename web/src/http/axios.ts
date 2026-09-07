import type { AxiosInstance, AxiosRequestConfig } from "axios"
import axios from "axios"
import { get, merge } from "lodash-es"

/**
 * OpenCR 的后端接口直接返回业务 JSON，不套 { code, data } 信封。
 *
 * 模板原本按信封解析，这里改成按 HTTP 状态码判断：接口本来就是给脚本共用的契约
 * （README 里有 curl 示例），为了迁就前端再包一层是没必要的间接层。
 */
function createInstance() {
  const instance = axios.create()

  instance.interceptors.response.use(
    response => response.data,
    (error) => {
      const status = get(error, "response.status")
      const message = get(error, "response.data.error")
      switch (status) {
        case 400:
          error.message = message || "请求参数有误"
          break
        case 401:
          // 未登录或登录已过期。不在这里跳转登录页：
          // 有些接口（例如查看 Finding 正文）对 Guest 返回 401 属于预期，
          // 由调用方自行决定是提示还是跳转。
          error.message = message || "需要登录"
          break
        case 403:
          error.message = message || "拒绝访问"
          break
        case 404:
          error.message = message || "资源不存在"
          break
        case 503:
          error.message = message || "服务暂不可用"
          break
        default:
          error.message = message || `请求失败（${status ?? "网络错误"}）`
      }
      return Promise.reject(error)
    }
  )
  return instance
}

function createRequest(instance: AxiosInstance) {
  return <T>(config: AxiosRequestConfig): Promise<T> => {
    const defaultConfig: AxiosRequestConfig = {
      baseURL: import.meta.env.VITE_BASE_URL,
      headers: { "Content-Type": "application/json" },
      timeout: 30000,
      // 登录态是 HttpOnly 的 session cookie，必须带上
      withCredentials: true
    }
    return instance(merge(defaultConfig, config))
  }
}

const instance = createInstance()

export const request = createRequest(instance)
