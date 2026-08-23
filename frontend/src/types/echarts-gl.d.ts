// echarts-gl 没有官方类型声明（npm 包无 types 字段）。它通过副作用注册
// 3D 系列（scatter3D/bar3D/surface 等）到 echarts，无需导出任何 API，
// 因此声明为 side-effect-only 模块即可。
declare module "echarts-gl";
