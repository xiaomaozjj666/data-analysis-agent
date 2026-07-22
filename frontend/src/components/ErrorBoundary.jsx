import React, { Component } from "react";

class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null, resetKey: 0 };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error("工作台渲染失败：", error, info);
  }

  reset = () => {
    // 递增 resetKey 强制子树重挂，清掉可能导致再次抛错的内部 state。
    this.setState((prev) => ({ error: null, resetKey: prev.resetKey + 1 }));
  };

  render() {
    if (this.state.error) {
      return (
        <main className="auth-gate">
          <div className="auth-card">
            <span className="section-kicker">DATA DESK</span>
            <h1>渲染出现异常</h1>
            <p>{String(this.state.error?.message || this.state.error || "未知错误")}</p>
            <button className="primary" type="button" onClick={() => window.location.reload()}>
              刷新页面
            </button>
            <button type="button" onClick={this.reset} style={{ marginTop: 8 }}>
              尝试恢复
            </button>
          </div>
        </main>
      );
    }
    return <div key={this.state.resetKey}>{this.props.children}</div>;
  }
}

export default ErrorBoundary;
