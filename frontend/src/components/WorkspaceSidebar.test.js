import { describe, it, expect } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import WorkspaceSidebar from "./WorkspaceSidebar.vue";
const sessions = [
  { id: "s1", title: "十月销售对比", project_id: "legacy-project" },
  { id: "s2", title: "临时分析" },
];
const render = (props = {}) =>
  mount(WorkspaceSidebar, {
    attachTo: document.body,
    props: { sessions, sessionId: "s1", ...props },
    global: { stubs: { "router-link": { template: "<a><slot /></a>" } } },
  });
describe("WorkspaceSidebar", () => {
  it("keeps all existing sessions visible without project controls", async () => {
    const w = render();
    expect(w.findAll(".session-link")).toHaveLength(2);
    expect(w.find('[aria-current="page"]').text()).toBe("十月销售对比");
    expect(w.find(".project-section").exists()).toBe(false);
    await w.findAll(".session-link")[1].trigger("click");
    expect(w.emitted("select-session")[0]).toEqual(["s2"]);
    w.unmount();
  });
  it("searches in a modal without filtering the sidebar, then opens the selected session", async () => {
    const w = render();
    const dialog = w.find("#session-search");
    expect(dialog.element.open).toBe(false);
    await w.find('[aria-label="搜索会话"]').trigger("click");
    await flushPromises();
    expect(dialog.element.open).toBe(true);
    await dialog.find("input").setValue(" 临时 ");
    expect(dialog.findAll(".search-result")).toHaveLength(1);
    expect(dialog.find(".search-result").text()).toContain("临时分析");
    expect(w.findAll(".session-link")).toHaveLength(2);
    await dialog.find(".search-result").trigger("click");
    await flushPromises();
    expect(w.emitted("select-session")[0]).toEqual(["s2"]);
    expect(dialog.element.open).toBe(false);
    w.unmount();
  });

  it("supports arrow navigation and Enter selection without submitting during Chinese composition", async () => {
    const w = render();
    await w.find('[aria-label="搜索会话"]').trigger("click");
    await flushPromises();
    const dialog = w.find("#session-search");
    const input = dialog.find("input");
    await input.trigger("keydown", { key: "ArrowDown" });
    expect(input.attributes("aria-activedescendant")).toBe("session-search-result-1");
    await input.trigger("keydown", { key: "Enter", isComposing: true });
    expect(w.emitted("select-session")).toBeUndefined();
    expect(dialog.element.open).toBe(true);
    await input.trigger("keydown", { key: "Enter" });
    await flushPromises();
    expect(w.emitted("select-session")[0]).toEqual(["s2"]);
    expect(dialog.element.open).toBe(false);
    w.unmount();
  });

  it("closes search with the close button, native Escape cancellation or backdrop and resets the query", async () => {
    const w = render();
    const dialog = w.find("#session-search");
    for (const dismiss of ["close", "escape", "backdrop"]) {
      await w.find('[aria-label="搜索会话"]').trigger("click");
      await flushPromises();
      expect(dialog.find("input").element.value).toBe("");
      expect(dialog.findAll(".search-result")).toHaveLength(2);
      await dialog.find("input").setValue("找不到的会话");
      expect(dialog.findAll(".search-result")).toHaveLength(0);
      expect(dialog.find('[role="status"]').text()).toBe("没有匹配的结果");
      await dialog.find("input").trigger("keydown", { key: "Enter" });
      expect(w.emitted("select-session")).toBeUndefined();
      await dialog.find(".search-dialog-content").trigger("click");
      expect(dialog.element.open).toBe(true);
      if (dismiss === "close") await dialog.find('[aria-label="关闭搜索弹窗"]').trigger("click");
      else if (dismiss === "escape") await dialog.trigger("cancel");
      else await dialog.trigger("click");
      await flushPromises();
      expect(dialog.element.open).toBe(false);
      expect(w.findAll(".session-link")).toHaveLength(2);
    }
    w.unmount();
  });

  it("keeps title matching case-insensitive and includes untitled sessions", async () => {
    const w = render({ sessions: [{ id: "english", title: "Monthly SALES" }, { id: "untitled", title: "" }] });
    await w.find('[aria-label="搜索会话"]').trigger("click");
    const dialog = w.find("#session-search");
    await dialog.find("input").setValue(" sales ");
    expect(dialog.findAll(".search-result")).toHaveLength(1);
    expect(dialog.find(".search-result").text()).toBe("Monthly SALES");
    await dialog.find("input").setValue("新会话");
    expect(dialog.findAll(".search-result")).toHaveLength(1);
    await dialog.find(".search-result").trigger("click");
    expect(w.emitted("select-session")[0]).toEqual(["untitled"]);
    w.unmount();
  });

  it("distinguishes loading from an empty session list", async () => {
    const w = render({ sessions: [], busy: true });
    await w.find('[aria-label="搜索会话"]').trigger("click");
    const dialog = w.find("#session-search");
    expect(dialog.find('[role="status"]').text()).toBe("载入中…");
    await w.setProps({ busy: false });
    expect(dialog.find('[role="status"]').text()).toBe("暂无会话");
    w.unmount();
  });
});
