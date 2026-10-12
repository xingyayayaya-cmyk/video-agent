import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

// 出海 AI 短视频审核平台 · 路由表
//   创作者：video/submit（上传）
//   审核员：video/reviews（复核队列）、video/reviews/:id（工作台）、video/dashboard（看板）
const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { public: true },
    },
    {
      path: '/',
      component: () => import('@/components/layout/AppLayout.vue'),
      meta: { requiresAuth: true },
      children: [
        {
          path: '',
          redirect: '/video/reviews',
        },
        {
          path: 'dashboard',
          name: 'dashboard',
          component: () => import('@/views/DashboardView.vue'),
        },
        // 创作者端 · 视频上传
        {
          path: 'video/submit',
          name: 'video-submit',
          component: () => import('@/views/video/VideoSubmitView.vue'),
          meta: { requiresCreator: true },
        },
        // 创作者端 · 提交详情（含原视频回放）
        {
          path: 'video/submissions/:submissionId',
          name: 'video-submission-detail',
          component: () => import('@/views/video/SubmissionDetailView.vue'),
          meta: { requiresCreator: true },
        },
        // 审核员端 · 复核队列
        {
          path: 'video/reviews',
          name: 'video-reviews',
          component: () => import('@/views/video/VideoReviewListView.vue'),
          meta: { requiresReviewer: true },
        },
        // 审核员端 · 审核工作台
        {
          path: 'video/reviews/:submissionId',
          name: 'video-review-detail',
          component: () => import('@/views/video/VideoReviewDetailView.vue'),
          meta: { requiresReviewer: true },
        },
        // 审核员端 · 审核看板
        {
          path: 'video/dashboard',
          name: 'video-dashboard',
          component: () => import('@/views/video/VideoDashboardView.vue'),
          meta: { requiresReviewer: true },
        },
      ],
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/video/reviews',
    },
  ],
})

router.beforeEach((to, _from, next) => {
  const auth = useAuthStore()

  if (to.meta.public) {
    if (auth.isLoggedIn && to.name === 'login') return next('/video/reviews')
    return next()
  }

  if (!auth.isLoggedIn) return next('/login')

  // 角色守卫：无权访问时回退到该角色可用的首页
  if (to.meta.requiresCreator && !auth.isCreator) {
    return next(auth.isReviewer ? '/video/reviews' : '/login')
  }
  if (to.meta.requiresReviewer && !auth.isReviewer) {
    return next(auth.isCreator ? '/video/submit' : '/login')
  }

  next()
})

export default router
