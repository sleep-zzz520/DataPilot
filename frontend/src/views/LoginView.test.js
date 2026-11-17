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

  it('重复使用同一个管理员测试账号登录，不注册新账号', async () => {
    authApi.login.mockResolvedValue({ token: 'test-token', username: 'admin', role: 'admin' })
    const wrapper = mount(LoginView)
    expect(wrapper.get('.test-account-btn').text()).toBe('使用管理员测试账号登录')
    expect(wrapper.get('.test-credentials').text()).toContain('admin')

    for (let i = 0; i < 2; i++) {
      await wrapper.get('.test-account-btn').trigger('click')
      await flushPromises()
    }
    expect(authApi.login.mock.calls).toEqual([['admin', 'admin1234'], ['admin', 'admin1234']])
    expect(authApi.register).not.toHaveBeenCalled()
    expect(localStorage.getItem('token')).toBe('test-token')
    expect(localStorage.getItem('username')).toBe('admin')
    expect(localStorage.getItem('role')).toBe('admin')
    expect(push).toHaveBeenCalledWith('/chat')
  })

  it('登录期间禁止重复提交和切换模式，失败后重试也不注册账号', async () => {
    let rejectRequest
    authApi.login.mockImplementationOnce(() => new Promise((resolve, reject) => { rejectRequest = reject }))
    const wrapper = mount(LoginView)
    await wrapper.get('#login-username').setValue('my-draft')
    await wrapper.get('#login-password').setValue('draft-password')
    await wrapper.get('.test-account-btn').trigger('click')
    expect(wrapper.get('.test-account-btn').text()).toBe('正在登录...')
    expect(wrapper.get('.login-btn').attributes('disabled')).toBeDefined()
    expect(wrapper.get('.login-switch button').attributes('disabled')).toBeDefined()
    await wrapper.get('form').trigger('submit')
    await wrapper.get('.test-account-btn').trigger('click')
    expect(authApi.login).toHaveBeenCalledOnce()
    expect(authApi.register).not.toHaveBeenCalled()

    rejectRequest({ suggestion: '账号不存在或密码错误' })
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toBe('账号不存在或密码错误')
    expect(localStorage.getItem('token')).toBeNull()
    expect(push).not.toHaveBeenCalled()
    expect(wrapper.get('.test-account-btn').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('#login-username').element.value).toBe('my-draft')
    expect(wrapper.get('#login-password').element.value).toBe('draft-password')

    authApi.login.mockResolvedValueOnce({ token: 'retry-token', username: 'admin', role: 'admin' })
    await wrapper.get('.test-account-btn').trigger('click')
    await flushPromises()
    expect(authApi.login).toHaveBeenCalledTimes(2)
    expect(authApi.register).not.toHaveBeenCalled()
    expect(localStorage.getItem('role')).toBe('admin')
    expect(push).toHaveBeenCalledWith('/chat')
  })

  it('注册页面的测试入口仍然登录已有账号', async () => {
    const wrapper = mount(LoginView)
    await wrapper.get('.login-switch button').trigger('click')
    authApi.login.mockResolvedValueOnce({ token: 'test-token', username: 'admin', role: 'admin' })
    await wrapper.get('.test-account-btn').trigger('click')
    await flushPromises()
    expect(authApi.login).toHaveBeenCalledWith('admin', 'admin1234')
    expect(authApi.register).not.toHaveBeenCalled()
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
