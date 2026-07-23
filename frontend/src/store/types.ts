// setValue/updater 联合类型：与原 useState 行为一致，setter 既接受直接值
// 也接受 functional updater。各 slice store 共用此类型。
export type Updater<T> = T | ((prev: T) => T);
