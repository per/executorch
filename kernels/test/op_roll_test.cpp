/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the BSD-style license found in the
 * LICENSE file in the root directory of this source tree.
 */

#include <executorch/kernels/test/FunctionHeaderWrapper.h> // Declares the operator
#include <executorch/kernels/test/TestUtil.h>
#include <executorch/runtime/core/exec_aten/exec_aten.h>
#include <executorch/runtime/core/exec_aten/testing_util/tensor_factory.h>
#include <executorch/runtime/core/exec_aten/testing_util/tensor_util.h>
#include <executorch/runtime/platform/runtime.h>

#include <gtest/gtest.h>

using namespace ::testing;
using executorch::aten::ArrayRef;
using executorch::aten::ScalarType;
using executorch::aten::Tensor;
using torch::executor::testing::TensorFactory;

Tensor& op_roll_out(
    const Tensor& input,
    ArrayRef<int64_t> shifts,
    ArrayRef<int64_t> dims,
    Tensor& out) {
  executorch::ET_RUNTIME_NAMESPACE::KernelRuntimeContext context{};
  return torch::executor::aten::roll_outf(context, input, shifts, dims, out);
}

class OpRollOutTest : public ::testing::Test {
 protected:
  void SetUp() override {
    // Since these tests cause ET_LOG to be called, the PAL must be initialized
    // first.
    torch::executor::runtime_init();
  }

  template <ScalarType DTYPE>
  void test_dtype() {
    TensorFactory<DTYPE> tf;

    Tensor input = tf.make({4, 2}, {1, 2, 3, 4, 5, 6, 7, 8});
    int64_t shifts_data[2] = {2, 1};
    ArrayRef<int64_t> shifts = ArrayRef<int64_t>(shifts_data, 2);
    int64_t dims_data[2] = {0, 1};
    ArrayRef<int64_t> dims = ArrayRef<int64_t>(dims_data, 2);
    Tensor out = tf.zeros({4, 2});
    Tensor out_expected = tf.make({4, 2}, {6, 5, 8, 7, 2, 1, 4, 3});
    op_roll_out(input, shifts, dims, out);
    EXPECT_TENSOR_CLOSE(out, out_expected);
  }
};

TEST_F(OpRollOutTest, SmokeTest) {
#define TEST_ENTRY(ctype, dtype) test_dtype<ScalarType::dtype>();
  // TODO: enable bool test after #7856 lands.
  ET_FORALL_REALHBF16_TYPES(TEST_ENTRY);
#undef TEST_ENTRY
}
