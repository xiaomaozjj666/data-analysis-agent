import React, { useCallback, useEffect, useRef, useState } from "react";

// React Bits 风格组件：Animated List —— 可滚动列表，新项加入时有滑入动画，
// 顶部/底部渐变遮罩，键盘箭头导航。零依赖（纯 CSS 动画）。
// 用于历史会话列表等需要平滑滚动的长列表。

interface AnimatedListProps<T> {
  items: T[];
  /** 渲染单个列表项 */
  renderItem: (item: T, index: number) => React.ReactNode;
  /** 选中项 key */
  selectedKey?: string;
  /** 获取项的唯一 key */
  getItemKey: (item: T, index: number) => string;
  className?: string;
  /** 最大高度 px，超出滚动 */
  maxHeight?: number;
  /** 是否显示顶底渐变 */
  showGradients?: boolean;
}

function AnimatedList<T>({
  items,
  renderItem,
  selectedKey,
  getItemKey,
  className = "",
  maxHeight,
  showGradients = true,
}: AnimatedListProps<T>) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [showTopFade, setShowTopFade] = useState(false);
  const [showBottomFade, setShowBottomFade] = useState(false);

  const checkScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    setShowTopFade(el.scrollTop > 4);
    setShowBottomFade(el.scrollTop + el.clientHeight < el.scrollHeight - 4);
  }, []);

  useEffect(() => {
    checkScroll();
  }, [items, checkScroll]);

  return (
    <div
      className={`rb-animated-list ${className}`}
      style={maxHeight ? { maxHeight } : undefined}
    >
      {showGradients && showTopFade && <div className="rb-list-fade rb-list-fade-top" />}
      <div
        className="rb-list-scroll"
        ref={scrollRef}
        onScroll={checkScroll}
      >
        {items.map((item, index) => {
          const key = getItemKey(item, index);
          const isSelected = selectedKey === key;
          return (
            <div
              key={key}
              className={`rb-list-item ${isSelected ? "is-selected" : ""}`}
              style={{ animationDelay: `${Math.min(index * 0.04, 0.4)}s` }}
            >
              {renderItem(item, index)}
            </div>
          );
        })}
      </div>
      {showGradients && showBottomFade && <div className="rb-list-fade rb-list-fade-bottom" />}
    </div>
  );
}

export default AnimatedList;
