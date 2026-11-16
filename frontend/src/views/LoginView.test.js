import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import LoginView from './LoginView.vue'
import * as authApi from '../api/auth.js'

const { push } = vi.hoisted(() => ({ push: vi.fn() }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push }) }))
vi.mock('../api/auth.js', () => ({ register: vi.fn(), login: vi.fn() }))

describe('LoginView', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    localStorage.clear()
  })

  it('快速添加生成独立账号，成功后保存登录信息并进入聊天', async () => {
    authApi.register.mockImplementation(async username => ({ token: 'test-token', username, role: 'user' }))
    const wrapper = mount(LoginView)
    await wrapper.get('.test-account-btn').trigger('click')
    await flushPromises()

    const [username, password] = authApi.register.mock.calls[0]
    expect(username).toMatch(/^test_[0-9a-f]{16}$/)
    expect(password).toMatch(/^[0-9a-f]{32}$/)
    expect(authApi.login).not.toHaveBeenCalled()
    expect(localStorage.getItem('token')).toBe('test-token')
    expect(localStorage.getItem('username')).toBe(username)
    expect(localStorage.getItem('role')).toBe('user')
    expect(push).toHaveBeenCalledWith('/chat')

    await wrapper.get('.test-account-btn').trigger('click')
    await flushPromises()
    expect(authApi.register.mock.calls[1][0]).not.toBe(username)
  })

  it('创建期间禁止重复提交和切换模式，失败后可以重试', async () => {
    let rejectRequest
    authApi.register.mockImplementationOnce(() => new Promise((resolve, reject) => { rejectRequest = reject }))
    const wrapper = mount(LoginView)
    await wrapper.get('.test-account-btn').trigger('click')
    expect(wrapper.get('.test-account-btn').text()).toBe('正在创建...')
    expect(wrapper.get('.login-btn').attributes('disabled')).toBeDefined()
    expect(wrapper.get('.login-switch button').attributes('disabled')).toBeDefined()
    await wrapper.get('form').trigger('submit')
    await wrapper.get('.test-account-btn').trigger('click')
    expect(authApi.register).toHaveBeenCalledOnce()
    expect(authApi.login).not.toHaveBeenCalled()

    rejectRequest({ suggestion: '服务暂不可用，请稍后再试' })
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toBe('服务暂不可用，请稍后再试')
    expect(localStorage.getItem('token')).toBeNull()
    expect(push).not.toHaveBeenCalled()
    expect(wrapper.get('.test-account-btn').attributes('disabled')).toBeUndefined()

    authApi.register.mockResolvedValueOnce({ token: 'retry-token', username: 'test_retry' })
    await wrapper.get('.test-account-btn').trigger('click')
    await flushPromises()
    expect(authApi.register).toHaveBeenCalledTimes(2)
    expect(localStorage.getItem('role')).toBe('user')
    expect(push).toHaveBeenCalledWith('/chat')
  })

  it('普通登录和注册保留原有提交路径，切换模式清除旧错误', async () => {
    const wrapper = mount(LoginView)
    await wrapper.get('#login-username').setValue('  analyst  ')
    await wrapper.get('#login-password').setValue('password123')
    authApi.login.mockRejectedValueOnce({ message: '用户名或密码错误' })
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(authApi.login).toHaveBeenCalledWith('analyst', 'password123')
    expect(wrapper.get('[role="alert"]').text()).toBe('用户名或密码错误')

    await wrapper.get('.login-switch button').trigger('click')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.get('#login-password').attributes('autocomplete')).toBe('new-password')
    authApi.register.mockResolvedValueOnce({ token: 'register-token', username: 'analyst', role: 'admin' })
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(authApi.register).toHaveBeenCalledWith('analyst', 'password123')
    expect(localStorage.getItem('role')).toBe('admin')
    expect(push).toHaveBeenCalledWith('/chat')
  })
})
